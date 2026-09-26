"""ch05 · 两个职责不同的子 Agent，以及它们与主 Agent 之间的上下文隔离。

拆分依据
--------
不是「任务听起来不一样」就拆，而是三件套**同时不同**才拆：

| 子 Agent | 数据源 | 工具集（最小权限） | 输出契约 |
|---|---|---|---|
| note-archivist | 本地笔记 | search_local_notes / read_local_note | 带路径+行号的引用清单，≤300 词 |
| web-scout | 开放网络 | tavily_search / think_tool | 带 URL 的发现清单，≤500 词 |

只要有一件相同就没必要拆——拆了主 Agent 更难路由，还多付一次上下文切换成本。

两条容易记错的继承规则（ch05 原文）
-----------------------------------
- ``tools``：不写 = 继承主 Agent 全部工具；**写了 = 完全替换，不合并**
- ``system_prompt`` / ``middleware`` / ``skills``：**都不继承**，必须各自写

主 Agent 侧不必手写 ``task`` 工具，``SubAgentMiddleware`` 会自动注入。
子 Agent 侧拿不到 ``task``（否则可以无限套娃）——这一点由探测脚本实测确认，
不靠推断。
"""

from __future__ import annotations

from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware import ToolCallLimitMiddleware

from research_deepagent.tools import (
    read_local_note,
    search_local_notes,
    tavily_search,
    think_tool,
)

ARCHIVIST_NAME = "note-archivist"
SCOUT_NAME = "web-scout"

# 子 Agent 是「搜 → 想 → 再搜」的自由循环，靠提示词约束不住；
# 与主图的护栏同理，这里给每个子 Agent 一个中间件级硬停。
MAX_SUBAGENT_TOOL_CALLS = 8

ARCHIVIST_PROMPT = """You are a local-notes archivist. You read only the user's own notes and project docs.

Workflow:
1. Call search_local_notes with 2-3 different keyword variants.
2. Open the most promising hits with read_local_note.
3. Return a citation list.

Output contract (hard):
- At most 300 words.
- One bullet per finding, each ending with `<path> L<start>-<end>`.
- No raw file dumps, no intermediate search output, no restating of the query.
"""

SCOUT_PROMPT = """You are a web scout. You search only the open web and have no access to local notes.

Workflow:
1. Run 2-4 tavily_search calls from different angles.
2. After each search, call think_tool to assess what is still missing.
3. Return a finding list.

Output contract (hard):
- At most 500 words.
- One bullet per finding, each carrying its source URL.
- No raw page content, no search transcripts.
"""

ORCHESTRATOR_PROMPT = """You coordinate two specialized sub-agents. Do not do their work yourself.

- `note-archivist` — local notes only. Returns citations with file paths and line numbers.
- `web-scout` — open web only. Returns findings with source URLs.

Routing:
- The question is about the user's own notes or prior write-ups → note-archivist
- The question needs current or external information → web-scout
- Both apply (e.g. compare local notes against the web) → delegate to both, one topic each

Use the task() tool and give each delegation exactly one topic.
After a sub-agent returns, integrate its summary. Do not re-derive its findings yourself.
"""

ARCHIVIST = {
    "name": ARCHIVIST_NAME,
    "description": (
        "Search the user's own local notes and project docs, then return a citation list "
        "with exact file paths and line numbers. Use when the answer may already be "
        "documented locally, or when the user asks what their own notes say. "
        "Offline — this agent has no web access."
    ),
    "system_prompt": ARCHIVIST_PROMPT,
    # 显式写出 = 完全替换主 Agent 工具集。archivist 拿不到 tavily_search，
    # 这是最小权限，不是遗漏。
    "tools": [search_local_notes, read_local_note],
    "middleware": [
        ToolCallLimitMiddleware(run_limit=MAX_SUBAGENT_TOOL_CALLS, exit_behavior="end"),
    ],
}

SCOUT = {
    "name": SCOUT_NAME,
    "description": (
        "Search the open web, cross-check sources, and return findings with source URLs. "
        "Use when the question needs current, external, or otherwise unavailable "
        "information. Has no access to local notes."
    ),
    "system_prompt": SCOUT_PROMPT,
    # 对称：scout 拿不到本地笔记工具。
    "tools": [tavily_search, think_tool],
    "middleware": [
        ToolCallLimitMiddleware(run_limit=MAX_SUBAGENT_TOOL_CALLS, exit_behavior="end"),
    ],
}

SUBAGENTS = [ARCHIVIST, SCOUT]


def _default_model() -> Any:
    """复用主图已配置好的模型对象，避免把 env→provider 的分支再抄一份。

    函数内 import：agent.py 在模块级就构建了整张主图，放在这里可以避免
    仅导入本模块就触发主图的构建副作用。
    """
    from research_deepagent.agent import model as project_model

    return project_model


def build_agent(model: Any | None = None):
    """两个子 Agent + 一个只负责协调的主 Agent。

    主 Agent 不挂 tavily_search / search_local_notes——它自己动手就等于
    绕过了隔离，子 Agent 也就白设了。
    """
    return create_deep_agent(
        model=model or _default_model(),
        system_prompt=ORCHESTRATOR_PROMPT,
        subagents=SUBAGENTS,
    )

"""ch04 实战：一个带任务规划的小型文档 Agent。

和第 2 章的 research agent 有两点不同：
1. **显式启用 `TodoListMiddleware`** —— v0.7 不再默认安装它（`deepagents` 的
   Codex harness profile 里写着 *"the SDK no longer provides it by default"*）。
   不传就没有 `write_todos` 工具，提示词里怎么要求都调不到。
2. **主 Agent 自己干活，不委派子代理** —— 单 Agent 形态，这样 `todos` 的
   状态流转能完整观察到，不会被 `task()` 隔在子代理里。

工具集刻意保持最小：一个搜索 + 两个文件工具 + `write_todos`。
文件工具走 `FilesystemBackend`，产物真落盘，跑完能用编辑器直接打开。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from dotenv import load_dotenv
from langchain.agents.middleware import TodoListMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.tools import tool

# 显式指向本项目的 .env
# 路径层级：文件在 src/research_deepagent/ch04/ 下，.env 在项目根 research_deepagent/
# parents[0]=ch04  [1]=research_deepagent(包) [2]=src  [3]=research_deepagent(项目根)
# —— load_dotenv() 默认按 cwd 找，从别处调用会读到不存在的 .env，报 Missing credentials
PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# 输出目录：文件工具落盘的根。Agent 写 /work/xxx.md 会落到 CONTENT_ROOT/work/xxx.md
# 选 frontend/public 是为了让产物能被前端同源预览到
# ---------------------------------------------------------------------------
CONTENT_ROOT = PROJECT_ROOT / "frontend" / "public"


SYSTEM_PROMPT = """你是一个文档撰写 Agent。

用户给你一个主题，你要产出一份结构完整、来源可核的 Markdown 文档。

## 工作方式

1. **先规划**：用 `write_todos` 把任务拆成 4 条左右的具体步骤，第一条立刻标为 `in_progress`
2. **逐步执行**：每完成一步就用 `write_todos` 更新状态（做完一条马上标 `completed`，不要攒着一起标）
3. **边查边存**：搜索到的关键事实先 `write_file` 落到 `/work/notes.md`，不要只留在对话里
4. **收尾**：把所有笔记整理成最终文档，写到 `/work/<主题slug>.md`
5. **交付**：最后一条 todo 标 `completed` 之后，再单独给一条消息说明产物路径和它在文档里引用了哪些来源

## 规划要求

- 每条 todo 是**可验证的具体动作**（「搜索 X 并整理出 3 条事实」），不是笼统的「做研究」
- 如果中途发现缺资料，可以改清单：删掉不必要的一条，或补一条新的
- 只有 2–3 步就能完成的任务，直接做，不要建清单

## 文档要求

- 用 `##` / `###` 分节，正文以段落为主
- 每个关键结论后面标 `[1]` `[2]` 这样的引用编号，结尾用 `### Sources` 统一列出
- 不确定的地方写明「未找到可靠来源」，不要编
"""


def _build_search_tool():
    """Tavily 搜索；没配 Key 时退化成占位工具，保证 demo 能跑通骨架。"""
    api_key = (os.getenv("TAVILY_API_KEY") or "").strip()

    if not api_key:

        @tool(parse_docstring=True)
        def search_docs(query: str, max_results: int = 5) -> str:
            """在互联网上检索资料。

            Args:
                query: 检索关键词。
                max_results: 最多返回多少条结果。

            Returns:
                检索结果文本；未配置 TAVILY_API_KEY 时返回提示。
            """
            return (
                "TAVILY_API_KEY 未配置，联网检索不可用。\n"
                "可以改用本地笔记：项目里已有 Learn-Notes/ 下的学习记录。"
            )

        return search_docs

    from tavily import TavilyClient

    client = TavilyClient(api_key=api_key)

    @tool(parse_docstring=True)
    def search_docs(
        query: str,
        max_results: int = 5,
        topic: Literal["general", "news"] = "general",
    ) -> str:
        """在互联网上检索资料，返回标题、URL 与正文摘要。

        Args:
            query: 检索关键词。
            max_results: 最多返回多少条结果。
            topic: 检索类别，general 为通用，news 为新闻。

        Returns:
            形如 `## 标题 / **URL:** ... / 摘要` 的文本。
        """
        resp = client.search(query, max_results=max_results, topic=topic)
        parts = []
        for r in resp.get("results", []):
            parts.append(f"## {r.get('title')}\n**URL:** {r.get('url')}\n\n{r.get('content')}\n\n---")
        return "\n".join(parts) or f"没有找到与 '{query}' 相关的结果。"

    return search_docs


def build_agent():
    """构造文档 Agent。返回 (agent, 输出目录)。"""
    model = init_chat_model(
        model=os.getenv("AGENTSEEK_MODEL") or "gpt-4.1-mini",
        model_provider="openai",
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_API_BASE"),
    )

    agent = create_deep_agent(
        model=model,
        tools=[_build_search_tool()],
        # ★ 本章主角：不传这个就没有 write_todos（v0.7 默认不装）
        middleware=[TodoListMiddleware()],
        system_prompt=SYSTEM_PROMPT,
        # 文件真落盘，方便用编辑器检查 agent 写了什么
        backend=FilesystemBackend(root_dir=CONTENT_ROOT, virtual_mode=True),
    )
    return agent, CONTENT_ROOT


def _fmt_todos(todos) -> str:
    """把 todos 渲染成一屏能看完的清单。"""
    if not todos:
        return "    (空)"
    icon = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
    return "\n".join(
        f"    {icon.get(t.get('status'), '[?]')} {t.get('content')}" for t in todos
    )


def run(task: str, *, thread_id: str = "ch04-demo", verbose: bool = True) -> dict:
    """跑一个文档任务，打印 todo 的每次变更与最终产物路径。"""
    agent, out_dir = build_agent()

    if verbose:
        print("=" * 78)
        print(f"任务：{task}")
        print("=" * 78)

    # 用 stream 才能看到 todos 的中间状态；invoke 只能拿到最后一版
    todo_snapshots: list[list] = []
    final_state: dict = {}
    for chunk in agent.stream(
        {"messages": [{"role": "user", "content": task}]},
        config={"configurable": {"thread_id": thread_id}, "recursion_limit": 120},
        stream_mode="values",
    ):
        final_state = chunk
        todos = chunk.get("todos")
        if todos and (not todo_snapshots or todos != todo_snapshots[-1]):
            todo_snapshots.append(list(todos))

    if verbose:
        print(f"\n--- todo 变更 {len(todo_snapshots)} 次 ---")
        for i, snap in enumerate(todo_snapshots, 1):
            print(f"\n  第 {i} 版：")
            print(_fmt_todos(snap))

        print("\n--- 产物 ---")
        produced = sorted(p for p in out_dir.rglob("*") if p.is_file() and ".vite" not in p.parts)
        for p in produced:
            print(f"    {p.relative_to(out_dir).as_posix()}  {p.stat().st_size} bytes")

    return {"state": final_state, "todos": todo_snapshots, "out_dir": out_dir}


if __name__ == "__main__":
    import sys

    topic = sys.argv[1] if len(sys.argv) > 1 else (
        "调研 Deep Agents 的 write_todos 机制：它解决什么问题、"
        "任务清单存在哪里、跨次调用怎么接续，写成一份简要文档。"
    )
    run(topic)

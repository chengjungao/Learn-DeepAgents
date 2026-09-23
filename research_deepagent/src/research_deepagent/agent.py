"""DeepAgents research graph, served by `agentseek-api dev`.

This module is pure deepagents + LangChain — no agentseek dependency. It
mirrors the upstream ``langchain-ai/deepagents/examples/deep_research/agent.py``
with these differences:
- ``init_chat_model`` is called with explicit ``model_provider=...`` so the
  generated app can target OpenAI, Anthropic, or Gemini from the same `.env`.
- The orchestrator and sub-agent constants are wired to cookiecutter
  variables so they can be tuned at scaffold time.
"""

from __future__ import annotations

import os
import warnings
from datetime import datetime
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from dotenv import load_dotenv
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.chat_models import init_chat_model

from research_deepagent.prompts import (
    RESEARCH_WORKFLOW_INSTRUCTIONS,
    RESEARCHER_INSTRUCTIONS,
    SUBAGENT_DELEGATION_INSTRUCTIONS,
)
from research_deepagent.tools import (
    NOTES_ROOT_DEFAULT,
    read_local_note,
    search_local_notes,
    tavily_search,
    think_tool,
)

# 显式指向项目根 .env：load_dotenv() 按 cwd 找，从别处（如仓库根）调用会读不到
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

SUPPORTED_MODEL_PROVIDERS = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google_genai",
    "google_genai": "google_genai",
    "gemini": "google_genai",
}


def _nonempty_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().replace("-", "_").lower()
    if normalized in SUPPORTED_MODEL_PROVIDERS:
        return SUPPORTED_MODEL_PROVIDERS[normalized]
    supported = ", ".join(sorted({"openai", "anthropic", "google_genai"}))
    raise ValueError(
        f"Unsupported AGENTSEEK_MODEL_PROVIDER={provider!r}. "
        f"Expected one of: {supported}."
    )


def _split_prefixed_model(model_name: str) -> tuple[str | None, str]:
    if ":" not in model_name:
        return None, model_name
    provider_candidate, bare_model = model_name.split(":", maxsplit=1)
    try:
        normalized_provider = _normalize_provider(provider_candidate)
    except ValueError:
        return None, model_name
    return normalized_provider, bare_model


DEFAULT_MODEL_RAW = (
    os.getenv("AGENTSEEK_MODEL")
    or os.getenv("DEEPAGENTS_MODEL")
    or os.getenv("BUB_MODEL")
    or "gpt-4.1-mini"
)
DEFAULT_MODEL_PROVIDER_RAW = os.getenv("AGENTSEEK_MODEL_PROVIDER")
DEFAULT_MODEL_PROVIDER_DEFAULT = "openai"

prefixed_model_provider, DEFAULT_MODEL = _split_prefixed_model(DEFAULT_MODEL_RAW)
if DEFAULT_MODEL_PROVIDER_RAW:
    MODEL_PROVIDER = _normalize_provider(DEFAULT_MODEL_PROVIDER_RAW)
    if prefixed_model_provider and prefixed_model_provider != MODEL_PROVIDER:
        raise ValueError(
            "AGENTSEEK_MODEL provider prefix does not match AGENTSEEK_MODEL_PROVIDER: "
            f"{DEFAULT_MODEL_RAW!r} vs {DEFAULT_MODEL_PROVIDER_RAW!r}."
        )
else:
    MODEL_PROVIDER = prefixed_model_provider or _normalize_provider(DEFAULT_MODEL_PROVIDER_DEFAULT)

# Some OpenAI-compatible gateways can pause for longer than LangChain OpenAI's
# default 120s chunk gap while streaming a large tool-call payload.
_stream_chunk_timeout_env = os.getenv("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S")
STREAM_CHUNK_TIMEOUT_S: float | None = 300.0
if _stream_chunk_timeout_env not in (None, ""):
    try:
        _parsed_timeout = float(_stream_chunk_timeout_env)
    except ValueError:
        warnings.warn(
            "Ignoring invalid LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S value; "
            "using the default 300s timeout instead.",
            stacklevel=2,
        )
    else:
        STREAM_CHUNK_TIMEOUT_S = None if _parsed_timeout <= 0 else _parsed_timeout

MAX_CONCURRENT_RESEARCH_UNITS = 3
MAX_RESEARCHER_ITERATIONS = 3

# --- 循环护栏（代码级硬约束）-------------------------------------------------
# 上面两个常量只被 format 进提示词，属于「建议」，模型可以无视；下面这些是
# 中间件级的硬停，超限时直接结束本轮并注入一条说明消息。
#
# 计量口径：
#   MAX_SUBAGENT_TOOL_CALLS      子代理单次委派内的工具调用总次数
#   MAX_SUBAGENT_MODEL_CALLS     子代理单次委派内的模型调用总次数
#   MAX_ORCHESTRATOR_MODEL_CALLS 主 Agent 单次运行内的模型调用总次数
#   MAX_ORCHESTRATOR_STEPS       主图步数（1 轮 model↔tools = 2 步）
#                                ⚠️ 该值只作用于主图，子代理读不到——见下方 graph 注释
#
# 取值依据（本机实测，非估算）：
#   一次「只查本地笔记、不联网」的简单调研 → 10 次工具调用 / 31 秒
#   复杂问题按 prompts.py 的预算：最多 5 次 web 搜索，每次搜索后强制 1 次
#   think_tool，再叠加本地笔记检索 → 正常最坏约 20~22 次工具调用
#   因此硬上限取 24（约正常最坏的 1.1 倍，留出重试余量），模型上限取 30。
#   调小会更早止损但可能截断正常调研；调大更宽松，代价是「看起来卡住」的时间更长。
MAX_SUBAGENT_TOOL_CALLS = 24
MAX_SUBAGENT_MODEL_CALLS = 30
MAX_ORCHESTRATOR_MODEL_CALLS = 24
MAX_ORCHESTRATOR_STEPS = 100

# --- 文件系统后端（2026-09-23 修，ch03 实验结论）-------------------------------
# 默认的 StateBackend 是 graph state 里的一块虚拟 FS：内置的 ls / glob / grep /
# read_file 全部只看得到虚拟 FS，看不见磁盘。后果是编排层想核实子代理引用的
# 本地文件时，glob 永远返回 "No files found"，把正确答案误判成编造。
#
# 改用 CompositeBackend 按路径前缀分流：
#   /notes/  → 真实磁盘的笔记根目录（只读语义由提示词约束，工具本身可写）
#   其余路径 → 仍留在 StateBackend，保持"草稿纸"的临时语义
#
# 为什么不用 FilesystemBackend 直接挂整个仓库根：实测它会把
# research_deepagent/.env（含真实密钥）一并交给模型，风险不可接受。
# 挂载后引用笔记必须带 /notes/ 前缀——挂载前缀会替换掉原来的路径语义。
NOTES_MOUNT = "/notes/"
NOTES_ROOT = Path(os.getenv("NOTES_ROOT") or NOTES_ROOT_DEFAULT)


def _build_backend() -> CompositeBackend:
    """构造文件系统后端：/notes/ 落真实磁盘，其余留在 state。"""
    return CompositeBackend(
        default=StateBackend(),
        routes={NOTES_MOUNT: FilesystemBackend(root_dir=NOTES_ROOT, virtual_mode=True)},
    )

current_date = datetime.now().strftime("%Y-%m-%d")

INSTRUCTIONS = (
    RESEARCH_WORKFLOW_INSTRUCTIONS
    + "\n\n"
    + "=" * 80
    + "\n\n"
    + SUBAGENT_DELEGATION_INSTRUCTIONS.format(
        max_concurrent_research_units=MAX_CONCURRENT_RESEARCH_UNITS,
        max_researcher_iterations=MAX_RESEARCHER_ITERATIONS,
    )
)

research_sub_agent = {
    "name": "research-agent",
    "description": (
        "Delegate research to the sub-agent researcher. "
        "Only give this researcher one topic at a time."
    ),
    "system_prompt": RESEARCHER_INSTRUCTIONS.format(date=current_date),
    "tools": [
        tavily_search,
        think_tool,
        # 本地知识检索：Tavily 负责联网，这两个负责用户自己的笔记与项目文档
        search_local_notes,
        read_local_note,
    ],
    # 硬性循环上限。子代理是「搜 → 想 → 再搜」的自由循环，仅靠提示词约束不住，
    # 超限时 exit_behavior="end" 会把已获得的结果连同终止原因交回编排层，
    # 让主 Agent 能基于现有材料收尾，而不是把整个 run 抛错。
    "middleware": [
        ToolCallLimitMiddleware(
            run_limit=MAX_SUBAGENT_TOOL_CALLS,
            exit_behavior="end",
        ),
        ModelCallLimitMiddleware(
            run_limit=MAX_SUBAGENT_MODEL_CALLS,
            exit_behavior="end",
        ),
    ],
}

MODEL_INIT_KWARGS: dict[str, object] = {
    "model": DEFAULT_MODEL,
    "model_provider": MODEL_PROVIDER,
}
if MODEL_PROVIDER == "openai":
    if _nonempty_env("OPENAI_API_KEY"):
        MODEL_INIT_KWARGS["api_key"] = _nonempty_env("OPENAI_API_KEY")
    if _nonempty_env("OPENAI_API_BASE"):
        MODEL_INIT_KWARGS["base_url"] = _nonempty_env("OPENAI_API_BASE")
    MODEL_INIT_KWARGS["stream_chunk_timeout"] = STREAM_CHUNK_TIMEOUT_S
elif MODEL_PROVIDER == "anthropic":
    if _nonempty_env("ANTHROPIC_API_KEY"):
        MODEL_INIT_KWARGS["api_key"] = _nonempty_env("ANTHROPIC_API_KEY")
    if _nonempty_env("ANTHROPIC_API_URL"):
        MODEL_INIT_KWARGS["base_url"] = _nonempty_env("ANTHROPIC_API_URL")
elif MODEL_PROVIDER == "google_genai":
    if _nonempty_env("GOOGLE_API_KEY"):
        MODEL_INIT_KWARGS["api_key"] = _nonempty_env("GOOGLE_API_KEY")
    if _nonempty_env("GOOGLE_API_BASE"):
        MODEL_INIT_KWARGS["base_url"] = _nonempty_env("GOOGLE_API_BASE")

model = init_chat_model(**MODEL_INIT_KWARGS)

graph = create_deep_agent(
    model=model,
    tools=[tavily_search, think_tool],
    system_prompt=INSTRUCTIONS,
    subagents=[research_sub_agent],
    # 文件系统后端：让内置文件工具能看见 /notes/ 下的真实笔记（见 _build_backend 注释）
    backend=_build_backend(),
    middleware=[
        # 编排层自保：主 Agent 反复委派 / 反复改写报告时，靠提示词里的
        # 「最多 3 轮委派」是拦不住的，这里给它一个真实的模型调用预算。
        ModelCallLimitMiddleware(
            run_limit=MAX_ORCHESTRATOR_MODEL_CALLS,
            exit_behavior="end",
        ),
        # 任务规划：v0.7 起不再默认安装，必须显式传入才有 write_todos。
        # RESEARCH_WORKFLOW_INSTRUCTIONS 第 1 步就要求「先调用 write_todos」，
        # 缺了它那条指令指向一个不存在的工具（实测报
        # "write_todos is not a valid tool"）。
        TodoListMiddleware(),
    ],
).with_config(
    # deepagents 与 langchain create_agent 都把 recursion_limit 硬编码为 9999
    # （langchain/agents/factory.py:1831，为兼容 middleware 链深度），这对研究型
    # agent 等于没有上限：1 轮 model↔tools 只算 2 步。最外层 with_config 可以覆盖它。
    # 注意：该值不会传导到子代理——子代理自身绑定了 9999，按 deepagents 的合并规则
    # 会赢下冲突（详见 subagents.py 的 ensure_config 注释），子代理只能靠 middleware 约束。
    {"recursion_limit": MAX_ORCHESTRATOR_STEPS}
)

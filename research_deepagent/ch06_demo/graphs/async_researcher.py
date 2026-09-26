"""ch06 · 故意很慢的异步子 Agent（Agent Protocol 服务上的一个 graph）。

固定 sleep，便于观察「主 Agent 立刻拿到 task ID 就返回」这件事。
sleep 秒数可用 CH06_SLEEP_SECONDS 覆盖。

这个模块必须是模块级变量 ``graph``——langgraph.json 以
``./graphs/async_researcher.py:graph`` 的形式引用它。
"""

from __future__ import annotations

import asyncio
import os

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph

SLEEP_SECONDS = float(os.environ.get("CH06_SLEEP_SECONDS", "6"))


async def slow_research(state: MessagesState) -> dict:
    last = state["messages"][-1].content if state["messages"] else "(no task)"
    await asyncio.sleep(SLEEP_SECONDS)
    return {
        "messages": [
            AIMessage(
                content=(
                    f"[async-researcher 完成，故意耗时 {SLEEP_SECONDS:.0f}s]\n"
                    f"收到的任务：{last}\n"
                    "结论：异步子 Agent 先返回 task id，在后台跑完，"
                    "随后可 check / update / cancel。"
                )
            )
        ]
    }


builder = StateGraph(MessagesState)
builder.add_node("slow_research", slow_research)
builder.add_edge(START, "slow_research")
builder.add_edge("slow_research", END)
graph = builder.compile()

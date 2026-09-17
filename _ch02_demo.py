"""ch02 实操验证：自定义工具 + 最小 Deep Agent 端到端跑通。

分两段跑：
  PART 1  直接 invoke 新加的两个工具（纯本地，不烧模型额度）
  PART 2  用一个最小 create_deep_agent 真跑一次，确认工具被模型绑上并真的被调用

用法（用项目自带的解释器，从工作区根跑即可 —— research_deepagent 是
editable 安装，路径无关）：

  cd D:\\Works\\DeepAgents学习
  unset TAVILY_API_KEY OPENAI_API_KEY
  research_deepagent/.venv/Scripts/python.exe _ch02_demo.py
"""

from __future__ import annotations

import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

from dotenv import load_dotenv

# load_dotenv() 默认从「当前工作目录」向上找 .env，脚本被移出项目目录后就找不到了。
# 显式指向项目的 .env，让脚本在任何 cwd 下都能跑。
_PROJECT_ENV = Path(__file__).resolve().parent / "research_deepagent" / ".env"
load_dotenv(_PROJECT_ENV if _PROJECT_ENV.is_file() else None)

from langchain.chat_models import init_chat_model
from deepagents import create_deep_agent

from research_deepagent.tools import read_local_note, search_local_notes


def hr(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# ---------------------------------------------------------------------------
# PART 1 — 工具直调
# ---------------------------------------------------------------------------
hr("PART 1  工具直调（本地、零 API 消耗）")

print("\n[1a] search_local_notes(query='write_todos')")
t0 = time.perf_counter()
res = search_local_notes.invoke({"query": "write_todos"})
print(f"     耗时 {time.perf_counter() - t0:.3f}s，返回 {len(res)} 字符")
print(res)

print("\n[1b] search_local_notes(query='StateBackend')")
print(search_local_notes.invoke({"query": "StateBackend"}))

print("\n[1c] read_local_note 边界测试：越界路径应被拒")
print(read_local_note.invoke({"path": "C:/Windows/System32/drivers/etc/hosts"}))

print("\n[1d] read_local_note 正常读取（带行号切片）")
print(read_local_note.invoke({"path": "Learn-Notes/Task1-AgentSeek环境搭建与踩坑.md",
                              "start_line": 320, "max_lines": 12}))

print("\n[1e] 空查询 / 不存在的关键词（错误处理）")
print(search_local_notes.invoke({"query": "   "}))
print(search_local_notes.invoke({"query": "zzz-绝对不会命中的词-zzz"}))

# ---------------------------------------------------------------------------
# PART 2 — 最小 Deep Agent 端到端
# ---------------------------------------------------------------------------
hr("PART 2  最小 Deep Agent 端到端")

model = init_chat_model(
    model=os.getenv("AGENTSEEK_MODEL", "glm-5.2"),
    model_provider="openai",
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("OPENAI_API_BASE"),
    stream_chunk_timeout=300.0,
)
print(f"模型: {os.getenv('AGENTSEEK_MODEL')} @ {os.getenv('OPENAI_API_BASE')}")

agent = create_deep_agent(
    model=model,
    tools=[search_local_notes, read_local_note],
    system_prompt=(
        "你是本地笔记助手。回答任何关于笔记内容的问题之前，"
        "必须先调用 search_local_notes 查证，并在答案里给出文件名与行号。"
    ),
)

question = "我的笔记里记录过一个「提示词要求调用、但工具集里并不存在」的工具，它是哪个？在哪一行？"
print(f"\n提问: {question}")

t0 = time.perf_counter()
result = agent.invoke({"messages": [{"role": "user", "content": question}]})
elapsed = time.perf_counter() - t0

msgs = result["messages"]
print(f"\n总耗时 {elapsed:.1f}s，消息数 {len(msgs)}")

print("\n--- 工具调用轨迹 ---")
for m in msgs:
    for tc in getattr(m, "tool_calls", None) or []:
        args = tc.get("args", {})
        shown = {k: (v[:60] + "..." if isinstance(v, str) and len(v) > 60 else v)
                 for k, v in args.items()}
        print(f"  -> {tc.get('name')}  {shown}")

print("\n--- 最终回答 ---")
print(msgs[-1].content)

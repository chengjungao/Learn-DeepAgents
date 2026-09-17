"""从已完成的线程 state 中提取 deepagents 虚拟文件系统里的报告，落盘保存。"""
from __future__ import annotations

import json
import urllib.request

BASE = "http://127.0.0.1:2024"
TID = "65d18ccc-d147-4354-9a35-21df9d8dbb91"
OUT = r"C:\Users\123\WorkBuddy\DeepAgents学习\langgraph-1.0-research-report.md"

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
state = json.loads(op.open(f"{BASE}/threads/{TID}/state", timeout=60).read())
vals = state.get("values") or {}

print("[state keys]", list(vals.keys()))

files = vals.get("files")
if not files:
    print("[warn] no 'files' key in state — 列出全部顶层键内容类型")
    for k, v in vals.items():
        if k == "messages":
            print(f"  {k}: {len(v)} messages")
        else:
            print(f"  {k}: {type(v).__name__} = {str(v)[:200]}")
else:
    print(f"[files] {len(files)} entries: {list(files.keys())}")

report = None
if isinstance(files, dict):
    for path, entry in files.items():
        if "final_report" in path:
            if isinstance(entry, dict):
                report = entry.get("content") or (entry.get("file_data") or {}).get("content")
            else:
                report = str(entry)
            break

if report:
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"[saved] {OUT}  ({len(report)} chars)")
else:
    print("[warn] report not found in files; dumping last AI message instead")
    msgs = vals.get("messages") or []
    for m in reversed(msgs):
        c = m.get("content")
        if (m.get("type") or m.get("role")) in ("ai", "assistant") and isinstance(c, str) and c.strip():
            with open(OUT, "w", encoding="utf-8") as f:
                f.write(c)
            print(f"[saved] {OUT}  ({len(c)} chars)")
            break

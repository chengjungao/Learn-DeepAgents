"""端到端验证脚本：通过 AgentSeek API 驱动 research graph 跑一次真实研究任务。

用法: python _e2e_research.py "<研究问题>"
输出: 实时打印 SSE 事件摘要，结束时打印最终报告全文。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:2024"
ASSISTANT_ID = "74d06aba-6a30-5f5b-88fe-7d81ce376129"
PROMPT = sys.argv[1] if len(sys.argv) > 1 else (
    "Research what LangGraph 1.0 added compared with 0.x. Cite sources."
)

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path: str, payload: dict, timeout: int = 1800):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return opener.open(req, timeout=timeout)


def get(path: str, timeout: int = 60):
    return opener.open(BASE + path, timeout=timeout)


t0 = time.time()
thread = json.loads(post("/threads", {}).read())
tid = thread["thread_id"]
print(f"[thread] {tid}", flush=True)
print(f"[prompt] {PROMPT}\n", flush=True)

payload = {
    "assistant_id": ASSISTANT_ID,
    "input": {"messages": [{"role": "user", "content": PROMPT}]},
    "stream_mode": ["updates"],
}

resp = post(f"/threads/{tid}/runs/stream", payload)
print(f"[run] started, streaming...\n", flush=True)

tool_calls = 0
node_hits: dict[str, int] = {}
last_ai_text = ""

cur_event = None
for raw in resp:
    line = raw.decode("utf-8", "replace").rstrip("\r\n")
    if line.startswith("event:"):
        cur_event = line.split(":", 1)[1].strip()
        continue
    if not line.startswith("data:"):
        continue
    data = line.split(":", 1)[1].strip()
    if data in ("", "null"):
        continue
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        continue

    if cur_event == "updates" and isinstance(obj, dict):
        for node, delta in obj.items():
            node_hits[node] = node_hits.get(node, 0) + 1
            msgs = (delta or {}).get("messages") if isinstance(delta, dict) else None
            if not msgs:
                continue
            msg = msgs[-1] if isinstance(msgs, list) and msgs else msgs
            if not isinstance(msg, dict):
                continue
            for tc in (msg.get("tool_calls") or []):
                tool_calls += 1
                name = tc.get("name")
                args = tc.get("args") or {}
                brief = args.get("query") or args.get("reflection") or ""
                brief = str(brief)[:90]
                print(f"  [{time.time()-t0:6.1f}s] {node} -> tool:{name}  {brief}", flush=True)
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                last_ai_text = content
                print(f"  [{time.time()-t0:6.1f}s] {node} -> text {len(content)} chars", flush=True)

    elif cur_event == "error":
        print(f"  !! ERROR: {data[:400]}", flush=True)

print(f"\n[stats] elapsed={time.time()-t0:.1f}s  tool_calls={tool_calls}  nodes={node_hits}", flush=True)

# 取回最终 state 里的最后一条 AI 消息
try:
    state = json.loads(get(f"/threads/{tid}/state").read())
    vals = state.get("values") or {}
    msgs = vals.get("messages") or []
    final = None
    for m in reversed(msgs):
        mtype = m.get("type") or m.get("role") or ""
        content = m.get("content")
        if mtype in ("ai", "assistant") and isinstance(content, str) and content.strip():
            final = content
            break
    if final is None and last_ai_text:
        final = last_ai_text
    print("\n" + "=" * 70)
    print("FINAL REPORT")
    print("=" * 70)
    print(final if final else "<no final AI message found>")
    print("=" * 70)
    print(f"[messages in state] {len(msgs)}")
except Exception as e:
    print(f"[warn] failed to fetch final state: {type(e).__name__}: {e}", flush=True)

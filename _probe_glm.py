"""Probe Zhipu GLM OpenAI-compatible endpoint.

Checks: (1) basic chat, (2) streaming, (3) tool calling — DeepAgents needs all three.
Reads credentials from the project .env so no key ends up in shell history.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).parent / "research_deepagent" / ".env"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def post(base: str, key: str, payload: dict, *, stream: bool = False, timeout: int = 120):
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def main() -> int:
    env = load_env(ENV_PATH)
    base = env.get("OPENAI_API_BASE", "")
    key = env.get("OPENAI_API_KEY", "")
    model = env.get("AGENTSEEK_MODEL", "")
    print(f"base={base}")
    print(f"model={model}")
    print(f"key_fingerprint={key[:8]}...{key[-6:]} (len={len(key)})")
    print("-" * 70)

    failures = 0

    # 1. basic non-streaming chat
    print("[1/3] basic chat ...", flush=True)
    t0 = time.time()
    try:
        resp = post(base, key, {
            "model": model,
            "messages": [{"role": "user", "content": "回答两个字：收到"}],
        })
        body = json.loads(resp.read().decode("utf-8"))
        text = body["choices"][0]["message"].get("content", "")
        print(f"  OK  {time.time() - t0:.2f}s  content={text!r}")
        print(f"  usage={body.get('usage')}")
    except urllib.error.HTTPError as exc:
        failures += 1
        print(f"  FAIL HTTP {exc.code}: {exc.read().decode('utf-8')[:400]}")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"  FAIL {type(exc).__name__}: {exc}")

    # 2. streaming
    print("[2/3] streaming ...", flush=True)
    t0 = time.time()
    try:
        resp = post(base, key, {
            "model": model,
            "messages": [{"role": "user", "content": "从1数到5，只输出数字，空格分隔"}],
            "stream": True,
        })
        chunks = 0
        first_delta_at = None
        raw = b""
        for line in resp:
            raw += line
            if line.startswith(b"data: ") and b"[DONE]" not in line:
                chunks += 1
                if first_delta_at is None:
                    first_delta_at = time.time() - t0
        print(f"  OK  {time.time() - t0:.2f}s  chunks={chunks}  ttft={first_delta_at:.2f}s"
              if first_delta_at else f"  OK  {time.time() - t0:.2f}s  chunks={chunks}")
    except urllib.error.HTTPError as exc:
        failures += 1
        print(f"  FAIL HTTP {exc.code}: {exc.read().decode('utf-8')[:400]}")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"  FAIL {type(exc).__name__}: {exc}")

    # 3. tool calling
    print("[3/3] tool call ...", flush=True)
    t0 = time.time()
    try:
        resp = post(base, key, {
            "model": model,
            "messages": [{"role": "user", "content": "北京现在天气怎么样？请调用工具查询。"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "查询指定城市的当前天气",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string", "description": "城市名"}},
                        "required": ["city"],
                    },
                },
            }],
            "tool_choice": "auto",
        })
        body = json.loads(resp.read().decode("utf-8"))
        msg = body["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if calls:
            fn = calls[0]["function"]
            print(f"  OK  {time.time() - t0:.2f}s  tool={fn['name']}  args={fn['arguments']}")
        else:
            failures += 1
            print(f"  FAIL no tool_calls. content={msg.get('content')!r}")
            print(f"       finish_reason={body['choices'][0].get('finish_reason')}")
        if msg.get("reasoning_content"):
            rc = msg["reasoning_content"]
            print(f"  note: reasoning_content present (len={len(rc)})")
    except urllib.error.HTTPError as exc:
        failures += 1
        print(f"  FAIL HTTP {exc.code}: {exc.read().decode('utf-8')[:400]}")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"  FAIL {type(exc).__name__}: {exc}")

    print("-" * 70)
    print(f"RESULT: {'ALL PASS' if failures == 0 else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

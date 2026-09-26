"""ch06 · 异步子 Agent 的 supervisor。

复用主项目已配置好的模型（`research_deepagent.agent.model`），
避免把 env→provider 的分支再抄一份。

⚠️ 实测结论：这里**必须**显式给 `url`。
不给 url 时 deepagents 走「ASGI 进程内传输」，`start_async_task` 的同步分支会在
`_ClientCache.get_sync()` 直接抛 ValueError（"has no url configured"），
异步分支则拿到 `get_client(url=None)`，在 agentseek-api 下报
`'NoneType' object is not callable`。原因见
`deepagents/middleware/async_subagents.py:211-234`：
`get_sync` 明确拒绝 url=None，而 ASGI 那条路需要一个进程内 ASGI app 引用，
agentseek-api 没有暴露。
填了 url 就走 HTTP 传输（ch06 说的「拆分部署 / 混合」拓扑），实测可用。

注意：不要在 `langgraph dev` / `agentseek-api dev` 下传 checkpointer，
平台已内置持久化，重复传会抛 ValueError。
"""

from __future__ import annotations

import os

from deepagents import AsyncSubAgent, create_deep_agent

from research_deepagent.agent import model

# 指向本服务自身。默认端口与 _ch06_async_live.py 保持一致。
ASYNC_URL = os.environ.get("CH06_ASYNC_URL", "http://127.0.0.1:2025")

SUPERVISOR_PROMPT = """You are a supervisor that delegates long-running work to a
background sub-agent named `researcher`.

Rules:
1. For any request that asks for research, delegation, or a background job, call
   `start_async_task` with subagent_type="researcher" immediately.
2. After starting a task, report the returned task_id to the user and STOP.
   Do NOT poll. Return control to the user right away.
3. Only call `check_async_task` or `list_async_tasks` when the user explicitly
   asks for progress.
4. If the user asks to change or extend a running task, call `update_async_task`.
5. If the user asks to stop a task, call `cancel_async_task`.
6. Always use the complete task_id. Never truncate, abbreviate, or rewrite it.
"""

graph = create_deep_agent(
    model=model,
    system_prompt=SUPERVISOR_PROMPT,
    subagents=[
        AsyncSubAgent(
            name="researcher",
            description=(
                "在后台执行长时研究任务。该 Agent 会故意等待数秒再返回，"
                "便于观察异步行为。需要不被阻塞地跑任务时使用。"
            ),
            graph_id="async-researcher",
            url=ASYNC_URL,
        )
    ],
)

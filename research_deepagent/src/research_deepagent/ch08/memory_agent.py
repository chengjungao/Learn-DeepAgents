"""ch08 · 长期记忆：让新对话也能读到用户偏好。

三种"记忆"的边界
----------------
- **短期**（thread 内）：``StateBackend`` 把文件存进 graph state，靠 Checkpointer 在
  同一个 ``thread_id`` 内持久化。换 thread 就没了。
- **长期**（跨 thread）：``StoreBackend`` 落进 langgraph Store，按 namespace 隔离，
  新 thread 照样读得到。
- 两者用 ``CompositeBackend`` 按路径前缀组合起来：``/memories/`` 走 Store，其余留在 state。

三条容易记错的规则
------------------
1. ``memory=["/memories/preferences.md"]`` 是**读取**配置——框架把已有文件内容注入
   系统提示词。它**不会创建**文件：0.7.10 起缺失的记忆文件被静默跳过，路径也不会
   作为"已加载记忆"出现在提示词里。要固定写入位置，得在提示词里另行约定。
2. Agent 可见路径带挂载前缀（``/memories/preferences.md``），而 **Store key 不带前缀**
   （``/preferences.md``）。预置文件时写错 key，就会出现"文件明明 put 了，Agent 却看不到"。
3. 验证跨对话**必须换新 ``thread_id``**：同一 thread 继续对话会复用 state 里已有的
   ``memory_contents``，那不叫"重新加载"。这条是官方文档明确点出来的坑。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.utils import create_file_data
from langgraph.checkpoint.memory import InMemorySaver

MEMORY_MOUNT = "/memories/"
MEMORY_PATH = "/memories/preferences.md"
# ⚠️ Store 侧 key 要去掉挂载前缀（CompositeBackend 路由时剥掉 /memories/）
MEMORY_STORE_KEY = "/preferences.md"

DEFAULT_USER_ID = "local-user"

SEED_CONTENT = "# 用户偏好\n暂无记录。\n"


@dataclass
class UserContext:
    """用户身份。

    本地 ``agent.invoke()`` 里 ``rt.server_info`` 可能为空，所以 namespace 从
    ``context`` 取 user_id；部署到 LangSmith / LangGraph Server 时才会走
    ``server_info.user.identity``。
    """

    user_id: str = DEFAULT_USER_ID


def user_namespace(rt: Any) -> tuple[str, ...]:
    """用户级 namespace —— A 用户的偏好不会泄露给 B 用户。"""
    context = getattr(rt, "context", None)
    user_id = getattr(context, "user_id", None) or DEFAULT_USER_ID
    return (user_id, "memories")


SYSTEM_PROMPT = f"""You are a coding assistant that remembers what a user tells you.

Long-term memory lives in {MEMORY_PATH}. Its contents are loaded for you at the
start of every conversation, whichever thread you are on.

When the user explicitly asks you to remember a preference:
1. Read {MEMORY_PATH} first.
2. Use edit_file to add the new entry — keep every existing entry, do not rewrite
   the file from scratch, and do not create a second preferences file.
3. Only after the tool call succeeds, tell the user it has been remembered.

Apply the preferences you find there without being asked.
"""


def _default_model() -> Any:
    """复用主项目已配置好的模型，避免重复维护 env→provider 分支。"""
    from research_deepagent.agent import model as project_model

    return project_model


def build_backend() -> CompositeBackend:
    """按路径前缀分流：``/memories/`` 跨对话保留，其余仍是"草稿纸"。"""
    return CompositeBackend(
        default=StateBackend(),
        routes={MEMORY_MOUNT: StoreBackend(namespace=user_namespace)},
    )


def build_agent(model: Any | None = None, store: Any | None = None, checkpointer: Any | None = None):
    """长期记忆 Agent。

    ``store`` 是记忆的落点：**同一个 store 实例被不同 thread 复用**，记忆才跨对话。
    每次重新构造都拿到空白记忆——这条在 demo 里最容易被误当成"记忆失效"。
    """
    if store is None:
        from langgraph.store.memory import InMemoryStore

        store = InMemoryStore()
    return create_deep_agent(
        model=model or _default_model(),
        context_schema=UserContext,
        store=store,
        checkpointer=checkpointer or InMemorySaver(),
        backend=build_backend(),
        memory=[MEMORY_PATH],
        system_prompt=SYSTEM_PROMPT,
    )


def seed_memory(
    store: Any,
    user_id: str = DEFAULT_USER_ID,
    content: str = SEED_CONTENT,
) -> bool:
    """预置偏好文件；已存在则原样返回，避免覆盖用户攒下的偏好。

    返回 ``True`` 表示这次真的写入了（首次初始化）。
    """
    namespace = (user_id, "memories")
    if store.get(namespace, MEMORY_STORE_KEY) is not None:
        return False
    store.put(namespace, MEMORY_STORE_KEY, create_file_data(content))
    return True


def read_memory(store: Any, user_id: str = DEFAULT_USER_ID) -> str | None:
    """从 Store 侧核对写入结果 —— 不能只看模型回复了一句"已记住"。"""
    item = store.get((user_id, "memories"), MEMORY_STORE_KEY)
    return None if item is None else item.value.get("content")

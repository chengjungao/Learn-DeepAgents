"""capstone · 个人知识库助手 —— 把课程六项能力装进一个能跑的 Agent。

能力矩阵
-------
| 能力 | 章节 | 落点 |
|---|---|---|
| 虚拟文件系统 + 多后端路由 | ch03 | `CompositeBackend`：``/notes/`` 真实磁盘、``/memories/`` 长期存储、``/skills/`` 技能目录 |
| 任务规划 | ch04 | `TodoListMiddleware` —— 复杂请求先 `write_todos` 拆解 |
| 子 Agent + 上下文隔离 | ch05 | `note-librarian` 子 Agent 专职检索，检索过程不进父上下文 |
| Skills | ch07 | ``/skills/note-format/SKILL.md`` 规定报告格式，按需加载 |
| 长期记忆 | ch08 | ``/memories/preferences.md`` 跨对话记住用户偏好 |
| Human-in-the-Loop | ch09 | 删除笔记 / 对外发布需人工审批 |

为什么挂在 CompositeBackend 上而不是三个独立 backend
-------------------------------------------------
`FilesystemMiddleware` 用的是**同一个 backend 实例**，子 Agent 也共用它 —— 所以
路由一次配好，主 Agent 与子 Agent 同时受益，不需要各配一套。这也是 ch03 实验的结论。

⚠️ 挂载前缀会替换路径语义：``/notes/`` 下直接是文件名（``/notes/foo.md``），
不是磁盘上的绝对路径。

设计取舍
-------
- 子 Agent 只给**只读**工具（`list_notes` / `read_note`）。"检索"这个职责本身不需要写权限，
  少一个写入口就少一类越权可能。
- 写操作留在主 Agent 手里，再叠一层 HITL 审批 —— 权限靠**架构**收，不靠提示词求。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend, StoreBackend
from langchain.agents.middleware import ModelCallLimitMiddleware, TodoListMiddleware, ToolCallLimitMiddleware
from langchain.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

# --------------------------------------------------------------------------
# 路径与后端
# --------------------------------------------------------------------------

CAPSTONE_ROOT = Path(__file__).resolve().parent
NOTES_DIR = CAPSTONE_ROOT / "notes"
SKILLS_DIR = CAPSTONE_ROOT / "skills"
PUBLISHED_DIR = CAPSTONE_ROOT / "published"

NOTES_MOUNT = "/notes/"
MEMORY_MOUNT = "/memories/"
SKILLS_MOUNT = "/skills/"
MEMORY_PATH = "/memories/preferences.md"
# ⚠️ Store 侧 key 要剥掉挂载前缀
MEMORY_STORE_KEY = "/preferences.md"

DEFAULT_USER_ID = "local-user"

MAX_ORCHESTRATOR_MODEL_CALLS = 20
MAX_SUBAGENT_TOOL_CALLS = 12
MAX_ORCHESTRATOR_STEPS = 60


@dataclass
class UserContext:
    """用户身份 —— namespace 从这里取 user_id，实现按用户隔离的记忆。"""

    user_id: str = DEFAULT_USER_ID


def user_namespace(rt: Any) -> tuple[str, ...]:
    context = getattr(rt, "context", None)
    user_id = getattr(context, "user_id", None) or DEFAULT_USER_ID
    return (user_id, "memories")


def build_backend() -> CompositeBackend:
    """三条路由 + 一块草稿纸。

    - ``/notes/``    → 真实磁盘的笔记目录（可读可写，但写操作受 HITL 约束）
    - ``/memories/`` → Store，跨对话保留
    - ``/skills/``   → 真实磁盘的技能目录，供 ``skills=`` 扫描
    - 其余路径       → StateBackend，一次对话内的草稿纸
    """
    return CompositeBackend(
        default=StateBackend(),
        routes={
            NOTES_MOUNT: FilesystemBackend(root_dir=NOTES_DIR, virtual_mode=True),
            MEMORY_MOUNT: StoreBackend(namespace=user_namespace),
            SKILLS_MOUNT: FilesystemBackend(root_dir=SKILLS_DIR, virtual_mode=True),
        },
    )


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

NOTE_SUFFIX = ".md"


def _note_path(name: str) -> Path:
    """笔记路径一律以 ``.md`` 结尾。

    这条**故意用函数归一，而不是在提示词里求模型守规矩**：实测模型保存草稿时会
    掉后缀（``搜索架构报告草稿``），而 ``list_notes`` 按 ``*.md`` 列文件，
    结果就是"草稿写进去了，但列表里看不见"——一个靠提示词堵不住的静默不一致。
    """
    filename = name.strip()
    if not filename.endswith(NOTE_SUFFIX):
        filename += NOTE_SUFFIX
    return NOTES_DIR / filename


@tool
def list_notes() -> str:
    """列出知识库里所有笔记的文件名。"""
    if not NOTES_DIR.is_dir():
        return "（知识库为空）"
    names = sorted(p.name for p in NOTES_DIR.glob(f"*{NOTE_SUFFIX}"))
    return "\n".join(names) if names else "（知识库为空）"


@tool
def read_note(name: str) -> str:
    """读取一篇笔记的完整内容。"""
    path = _note_path(name)
    if not path.is_file():
        return f"Error: 笔记 {name} 不存在"
    return path.read_text(encoding="utf-8")


@tool
def save_note(name: str, content: str) -> str:
    """新建或覆盖一篇笔记（内部草稿，不需要审批）。"""
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    path = _note_path(name)
    path.write_text(content, encoding="utf-8")
    return f"已保存笔记 {path.name}（{len(content)} 字）"


@tool
def delete_note(name: str) -> str:
    """删除一篇笔记。不可逆，需要人工审批。"""
    path = _note_path(name)
    if not path.is_file():
        return f"Error: 笔记 {name} 不存在"
    path.unlink()
    return f"已删除笔记 {path.name}"


@tool
def publish_report(name: str, title: str) -> str:
    """把一篇笔记发布到对外目录。发布后外部可见，需要人工审批。"""
    source = _note_path(name)
    if not source.is_file():
        return f"Error: 笔记 {name} 不存在"
    PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
    target = PUBLISHED_DIR / source.name
    target.write_text(f"# {title}\n\n{source.read_text(encoding='utf-8')}", encoding="utf-8")
    return f"已发布：{target.name}"


TOOLS = [list_notes, read_note, save_note, delete_note, publish_report]

# --------------------------------------------------------------------------
# 子 Agent：只给只读工具
# --------------------------------------------------------------------------

LITERARIAN = {
    "name": "note-librarian",
    "description": (
        "在**本地知识库**里检索笔记并回报命中段落。当问题需要依据用户自己的笔记"
        "（而不是凭空作答）时委派给它。它只读不写，一次只处理一个检索主题。"
    ),
    "system_prompt": (
        "你是知识库检索员。任务是在 /notes/ 下找到与问题相关的笔记并回报依据。\n"
        "先用 list_notes 看清有哪些笔记，再用 read_note 逐篇读取，不要凭文件名猜内容。\n"
        "输出格式：每条命中写成 `文件名 · 要点`，最多 5 条；要点必须能在原文中找到，"
        "不许改写或补充。一条都没命中就直说「知识库中没有相关内容」，不要编造。"
    ),
    "tools": [list_notes, read_note],
    "middleware": [
        ToolCallLimitMiddleware(run_limit=MAX_SUBAGENT_TOOL_CALLS, exit_behavior="end"),
    ],
}

# --------------------------------------------------------------------------
# HITL：按风险分层
# --------------------------------------------------------------------------

INTERRUPT_ON: dict[str, Any] = {
    # 高风险：对外可见 → 审批 / 改标题 / 拒绝
    "publish_report": {"allowed_decisions": ["approve", "edit", "reject"]},
    # 高风险：不可逆 → 审批 / 拒绝
    "delete_note": {"allowed_decisions": ["approve", "reject"]},
    # 低风险：只读与内部草稿，放行
    "list_notes": False,
    "read_note": False,
    "save_note": False,
}

# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------

SYSTEM_PROMPT = f"""你是用户的个人知识库助手。知识库是他自己写的笔记，存放在 {NOTES_MOUNT}
下（``/notes/xxx.md``）。

工作方式：
1. 接到需要多步完成的请求时，先用 write_todos 拆解，再逐步执行、随时更新清单。
2. 需要依据用户笔记作答时，委派给 note-librarian 子代理去检索——
   不要自己逐篇翻读，也不要替它猜内容。
3. 写报告/整理稿之前，先看一眼 {SKILLS_MOUNT} 下有没有该用的格式规范，按规范起草。
4. 用户明确要求记住偏好时，先读 {MEMORY_PATH}，再用 edit_file 追加条目
   （保留已有内容，不要重写整个文件）。写入成功后才回复「已记住」。
5. 删除笔记与对外发布都会暂停等待用户审批。被拒绝时不要重试同一个动作，
   换一个更稳妥的做法，或者直接说明已停止。

回答用简体中文。涉及笔记内容时，引用要带文件名。
"""


def _default_model() -> Any:
    from research_deepagent.agent import model as project_model

    return project_model


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


def build_agent(
    model: Any | None = None,
    store: Any | None = None,
    checkpointer: Any | None = None,
    with_planning: bool = True,
):
    """六项能力齐备的知识库助手。

    ``store`` 决定记忆是否跨对话：**同一实例复用于多个 thread** 时才跨对话。
    ``with_planning=False`` 可关掉 TodoListMiddleware，用于对比"有/无任务规划"的差别。
    """
    if store is None:
        from langgraph.store.memory import InMemoryStore

        store = InMemoryStore()

    middleware = [
        # 编排层自保：模型调用预算，挡住反复委派/反复改写
        ModelCallLimitMiddleware(
            run_limit=MAX_ORCHESTRATOR_MODEL_CALLS, exit_behavior="end"
        ),
    ]
    if with_planning:
        # v0.7 起 TodoListMiddleware 不再默认安装，必须显式传入才有 write_todos
        middleware.append(TodoListMiddleware())

    return create_deep_agent(
        model=model or _default_model(),
        tools=TOOLS,
        system_prompt=SYSTEM_PROMPT,
        subagents=[LITERARIAN],
        middleware=middleware,
        backend=build_backend(),
        skills=[SKILLS_MOUNT],
        memory=[MEMORY_PATH],
        context_schema=UserContext,
        store=store,
        checkpointer=checkpointer or InMemorySaver(),
        interrupt_on=INTERRUPT_ON,
    ).with_config({"recursion_limit": MAX_ORCHESTRATOR_STEPS})


def seed_memory(store: Any, user_id: str = DEFAULT_USER_ID) -> bool:
    """预置偏好文件（已存在则不动）。返回是否真的写入。"""
    from deepagents.backends.utils import create_file_data

    namespace = (user_id, "memories")
    if store.get(namespace, MEMORY_STORE_KEY) is not None:
        return False
    store.put(
        namespace,
        MEMORY_STORE_KEY,
        create_file_data("# 用户偏好\n暂无记录。\n"),
    )
    return True


def read_memory(store: Any, user_id: str = DEFAULT_USER_ID) -> str | None:
    item = store.get((user_id, "memories"), MEMORY_STORE_KEY)
    return None if item is None else item.value.get("content")

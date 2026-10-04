"""ch09 · Human-in-the-Loop：给敏感操作装一道审批闸门。

``interrupt_on`` 是工具级审批的便捷入口
-------------------------------------
按工具名映射三种配置值：``True``（全决策）、``False``（不拦）、
``{"allowed_decisions": [...]}``（只放指定决策）。

四种决策，别用混
----------------
| 决策 | 含义 | 用在哪 |
|---|---|---|
| ``approve`` | 按原参数执行 | 确认无风险 |
| ``edit`` | 改参数后执行 | 收件人/路径写错了 |
| ``reject`` | 跳过调用并把原因反馈给模型 | **拒绝副作用工具只能用它** |
| ``respond`` | 人代替工具返回一条"成功"结果 | 仅限 ``ask_user`` 这类询问型工具 |

⚠️ 拒绝删除/发送这类工具时**不要用 ``respond``**：它的 message 会被模型当作一次
成功的 ToolMessage 收下，模型会以为文件真删了。

恢复流程的四条硬要求（缺一条就恢复不了）
--------------------------------------
1. 必须配 Checkpointer —— HITL 靠它保存中断现场
2. 必须用**同一个 thread_id**
3. 必须 ``version="v2"``
4. ``decisions`` 的**数量和顺序**要与 ``action_requests`` 一一对应（批量中断时）

底层还有一条容易吃亏的规则：``Command(resume=...)`` 恢复时**节点从头重放**，
所以 ``interrupt()`` 之前的副作用必须幂等。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from deepagents import FilesystemPermission, create_deep_agent
from langchain.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

# 工具真实读写这个目录，演示完由脚本清理（别让它变成仓库垃圾）
WORKSPACE = Path(
    os.environ.get("CH09_WORKSPACE") or Path(__file__).resolve().parent / "_workspace"
)
OUTBOX = WORKSPACE / "_outbox"


# --------------------------------------------------------------------------
# 工具：按风险等级天然分成四类
# --------------------------------------------------------------------------


@tool
def list_notes() -> str:
    """列出笔记目录下的所有笔记文件名。"""
    if not WORKSPACE.is_dir():
        return "（笔记目录为空）"
    names = sorted(p.name for p in WORKSPACE.glob("*.md"))
    return "\n".join(names) if names else "（没有笔记）"


@tool
def read_note(name: str) -> str:
    """读取一篇笔记的内容。"""
    path = WORKSPACE / name
    if not path.is_file():
        return f"Error: 笔记 {name} 不存在"
    return path.read_text(encoding="utf-8")


@tool
def save_note(name: str, content: str) -> str:
    """新建或覆盖一篇笔记。"""
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (WORKSPACE / name).write_text(content, encoding="utf-8")
    return f"已保存笔记 {name}（{len(content)} 字）"


@tool
def delete_note(name: str) -> str:
    """删除一篇笔记。此操作不可逆。"""
    path = WORKSPACE / name
    if not path.is_file():
        return f"Error: 笔记 {name} 不存在"
    path.unlink()
    return f"已删除笔记 {name}"


@tool
def send_email(to: str, subject: str, body: str) -> str:
    """向指定收件人发送邮件（本演示落到本地 outbox，不真发）。"""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    stamp = len(list(OUTBOX.glob("*.txt"))) + 1
    record = OUTBOX / f"{stamp:02d}-{to.replace('@', '_at_')}.txt"
    record.write_text(f"To: {to}\nSubject: {subject}\n\n{body}\n", encoding="utf-8")
    return f"邮件已发送至 {to}（落盘记录 {record.name}）"


@tool
def ask_user(question: str) -> str:
    """向用户提问；真实回答由 HITL 的 ``respond`` 决策提供。"""
    return "等待用户回答"


TOOLS = [list_notes, read_note, save_note, delete_note, send_email, ask_user]


# --------------------------------------------------------------------------
# 条件中断：只拦真正危险的那部分调用
# --------------------------------------------------------------------------


def overwriting_existing_note(request: Any) -> bool:
    """``when`` 谓词：只有"覆盖已存在的笔记"才暂停，新建直接放行。

    不写 ``when`` 时，工具名一旦出现在 ``interrupt_on`` 里就每次都暂停；
    加上谓词后，审批界面只展示真正需要人决策的动作。
    """
    args = getattr(request, "tool_call", {}).get("args") or {}
    name = args.get("name", "")
    return bool(name) and (WORKSPACE / name).is_file()


# --------------------------------------------------------------------------
# 按风险等级分层
# --------------------------------------------------------------------------

INTERRUPT_ON: dict[str, Any] = {
    # 高风险：不可逆或影响外部系统 → 审批 / 改参数 / 拒绝
    "delete_note": {"allowed_decisions": ["approve", "edit", "reject"]},
    "send_email": {"allowed_decisions": ["approve", "edit", "reject"]},
    # 中风险：可审批、可拒绝，但不开放 edit（大幅改参数会让模型重新规划）
    "save_note": {
        "allowed_decisions": ["approve", "reject"],
        "when": overwriting_existing_note,
    },
    # 低风险：只读，直接放行
    "list_notes": False,
    "read_note": False,
    # 人工输入型：人的 message 会成为工具的成功结果
    "ask_user": {"allowed_decisions": ["respond"]},
}

SYSTEM_PROMPT = """You are a note-keeping assistant for a small team.

You can list, read, save, delete notes and send email. Deleting a note and
sending email are irreversible from the user's point of view, so the harness
will pause and ask for human approval before they run. When a call is paused,
wait for the decision — do not retry it on your own.

If the user refuses an action, respect the reason in the reply and pick a safer
alternative instead of trying the same call again.
"""


def _default_model() -> Any:
    from research_deepagent.agent import model as project_model

    return project_model


def build_agent(
    model: Any | None = None,
    checkpointer: Any | None = None,
    guard_secret_paths: bool = False,
):
    """HITL Agent。

    ``checkpointer`` 是硬要求：没有它，中断之后无法恢复。

    ``guard_secret_paths=True`` 时额外挂一条文件系统权限规则 —— 内置
    ``write_file`` / ``edit_file`` / ``delete`` 命中 ``/secrets/**`` 的写操作
    也会抛同样格式的中断。它与 ``interrupt_on`` 是**合并**生效的，一次人工审查
    就能同时覆盖自定义工具和受保护路径。
    """
    permissions = None
    if guard_secret_paths:
        permissions = [
            FilesystemPermission(
                operations=["write"],
                paths=["/secrets/**"],
                mode="interrupt",
            )
        ]
    return create_deep_agent(
        model=model or _default_model(),
        tools=TOOLS,
        system_prompt=SYSTEM_PROMPT,
        interrupt_on=INTERRUPT_ON,
        checkpointer=checkpointer or InMemorySaver(),
        permissions=permissions,
    )


# --------------------------------------------------------------------------
# 恢复用的决策构造助手：把"怎么写决策"从脚本里收进模块
# --------------------------------------------------------------------------


def describe_interrupt(interrupt_value: dict[str, Any]) -> list[dict[str, Any]]:
    """把中断负载整理成便于打印/断言的结构。

    字段名两种写法都存在：Deep Agents 侧用 ``args``，LangChain 标准
    ``HumanInTheLoopMiddleware`` 用 ``arguments``。这里统一读出来。
    """
    configs = {
        cfg["action_name"]: cfg for cfg in interrupt_value.get("review_configs", [])
    }
    rows = []
    for action in interrupt_value.get("action_requests", []):
        args = action.get("arguments", action.get("args", {}))
        rows.append(
            {
                "name": action["name"],
                "args": args,
                "allowed_decisions": configs.get(action["name"], {}).get(
                    "allowed_decisions", []
                ),
            }
        )
    return rows


def approve() -> dict[str, str]:
    return {"type": "approve"}


def reject(message: str) -> dict[str, str]:
    return {"type": "reject", "message": message}


def edit(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """改参数后执行。

    ⚠️ ``edited_action`` 里的参数键固定是 ``args``（不是 ``arguments``），
    且必须带上工具名。
    """
    return {"type": "edit", "edited_action": {"name": name, "args": args}}


def respond(message: str) -> dict[str, str]:
    return {"type": "respond", "message": message}

"""capstone · 演示脚本：六项课程能力一轮全跑。

流程
----
轮次 1  记住偏好（新 thread）        → 长期记忆写入 Store
轮次 2  整理一份对外报告（新 thread）→ 任务规划 + 委派子 Agent + 读 Skill + 写草稿
轮次 3  发布草稿（沿用轮次 2 的 thread）→ 触发 HITL，用 edit 改标题后放行
轮次 4  请求删除笔记（新 thread）    → 触发 HITL，用 reject 拒绝

每个环节都核对**磁盘/存储上的真实后果**，不只看模型说了什么。
演示产物（新写的草稿、发布的报告）跑完自动清理，仓库保持干净。

运行（在 research_deepagent/ 下）：

    .venv/Scripts/python.exe -m research_deepagent.capstone.demo
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

# demo.py 在 <项目根>/src/research_deepagent/capstone/ 下，往上第三层是项目根
PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")

from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.store.memory import InMemoryStore  # noqa: E402
from langgraph.types import Command  # noqa: E402

from research_deepagent.capstone import (  # noqa: E402
    NOTES_DIR,
    PUBLISHED_DIR,
    UserContext,
    build_agent,
    read_memory,
    seed_memory,
)

CHECKS: list[tuple[str, bool]] = []


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("-" * 78)


def snapshot(*dirs: Path) -> set[Path]:
    found: set[Path] = set()
    for directory in dirs:
        if directory.is_dir():
            found |= {p for p in directory.rglob("*") if p.is_file()}
    return found


def sweep(before: set[Path], *dirs: Path) -> list[Path]:
    """删掉本次新增的文件，再收走空目录（探针/演示产物不进仓库）。"""
    leaked = sorted(snapshot(*dirs) - before)
    for path in leaked:
        path.unlink()
    for directory in dirs:
        for child in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if child.is_dir() and not any(child.iterdir()):
                child.rmdir()
    return leaked


def interrupt_of(result: dict):
    """本地 invoke 返回 dict，中断挂在 result["__interrupt__"]。"""
    raw = result.get("__interrupt__") or []
    return raw[0].value if raw else None


def trace(result: dict) -> list[str]:
    rows = []
    for message in result["messages"]:
        for call in getattr(message, "tool_calls", None) or []:
            args = call.get("args") or {}
            brief = ", ".join(f"{k}={str(v)[:28]}" for k, v in list(args.items())[:3])
            rows.append(f"{call['name']}({brief})")
    return rows


def last_ai(result: dict) -> str:
    for message in reversed(result["messages"]):
        if isinstance(message, AIMessage) and message.content:
            return str(message.content)
    return ""


def todos_of(result: dict) -> list:
    return result.get("todos") or []


def run_turn(agent, config: dict, user, text: str) -> dict:
    return agent.invoke(
        {"messages": [{"role": "user", "content": text}]}, context=user, config=config
    )


def resume(agent, config: dict, user, decisions: list[dict]) -> dict:
    return agent.invoke(
        Command(resume={"decisions": decisions}), context=user, config=config
    )


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def main() -> int:
    print(f"知识库现有笔记：{sorted(p.name for p in NOTES_DIR.glob('*.md'))}")
    before = snapshot(NOTES_DIR, PUBLISHED_DIR)

    store = InMemoryStore()
    seed_memory(store)
    agent = build_agent(store=store)
    user = UserContext()

    # ---------------- 轮次 1：长期记忆 ----------------
    section("轮次 1 · 记住偏好（新 thread）")
    turn1 = "记住我的偏好：对外报告用简体中文写，要点控制在 5 条以内。"
    print(f"用户：{turn1}")
    result1 = run_turn(agent, {"configurable": {"thread_id": str(uuid4())}}, user, turn1)
    for row in trace(result1):
        print(f"  → {row}")

    memory = read_memory(store)
    print("  从 Store 侧核对：")
    if memory:
        for line in memory.strip().splitlines():
            print(f"    {line}")
    ok_memory = bool(memory) and "中文" in memory and ("5" in memory or "五" in memory)
    CHECKS.append(("轮次 1 · 偏好真的写进长期记忆", ok_memory))

    # ---------------- 轮次 2：规划 + 子 Agent + Skills ----------------
    section("轮次 2 · 整理对外报告（另一个新 thread）")
    thread2 = str(uuid4())
    config2 = {"configurable": {"thread_id": thread2}}
    turn2 = "把知识库里关于搜索架构演进的笔记整理成一份对外发布的报告草稿。"
    print(f"用户：{turn2}")
    result2 = run_turn(agent, config2, user, turn2)
    rows2 = trace(result2)
    for row in rows2:
        print(f"  → {row}")

    todos = todos_of(result2)
    print(f"  任务清单（{len(todos)} 项）：")
    for item in todos:
        status = item.get("status") if isinstance(item, dict) else getattr(item, "status", "?")
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", "?")
        print(f"    [{status}] {content}")
    CHECKS.append(("轮次 2 · 用了任务规划（write_todos）", bool(todos)))

    delegated = [r for r in rows2 if r.startswith("task(")]
    print(f"  委派子 Agent：{delegated or '（无）'}")
    CHECKS.append(("轮次 2 · 委派了 note-librarian 子代理", bool(delegated)))

    new_notes = sorted(p.name for p in snapshot(NOTES_DIR) - before)
    print(f"  新写入的草稿：{new_notes or '（无）'}")
    CHECKS.append(("轮次 2 · 产出了报告草稿", bool(new_notes)))

    # 技能是否被读到：Skill 的规范要求四个小节
    draft_text = ""
    for name in new_notes:
        draft_text += (NOTES_DIR / name).read_text(encoding="utf-8")
    follows_skill = all(k in draft_text for k in ("摘要", "要点", "引用"))
    print(f"  草稿是否含 Skill 规定的小节：{'是' if follows_skill else '否'}")
    CHECKS.append(("轮次 2 · 草稿遵循 note-format 技能", follows_skill))

    # ---------------- 轮次 3：HITL 发布（edit 改标题） ----------------
    section("轮次 3 · 发布草稿 —— 触发审批（沿用轮次 2 的 thread）")
    turn3 = "把刚才那份草稿发布出去。"
    print(f"用户：{turn3}")
    result3 = run_turn(agent, config2, user, turn3)
    payload = interrupt_of(result3)
    if payload is None:
        CHECKS.append(("轮次 3 · 发布触发中断", False))
        print("  ✗ 没有触发中断")
    else:
        request = payload["action_requests"][0]
        args = request.get("arguments", request.get("args", {}))
        configs = {c["action_name"]: c for c in payload.get("review_configs", [])}
        print(f"  中断动作：{request['name']}  参数：{args}")
        print(f"  可选决策：{configs.get(request['name'], {}).get('allowed_decisions')}")

        changed = dict(args)
        changed["title"] = "搜索平台五代演进：一份内部速览"
        print(f"  决策 edit：标题改成「{changed['title']}」")
        result3 = resume(
            agent,
            config2,
            user,
            [{"type": "edit", "edited_action": {"name": request["name"], "args": changed}}],
        )
        if interrupt_of(result3) is not None:
            print("  （后续仍有中断，自动批准以推进）")
            result3 = resume(agent, config2, user, [{"type": "approve"}])

        published = sorted(p.name for p in PUBLISHED_DIR.iterdir() if p.is_file())
        print(f"  发布目录：{published}")
        head = ""
        for name in published:
            head = (PUBLISHED_DIR / name).read_text(encoding="utf-8").splitlines()[0]
        print(f"  已发布文件首行：{head}")
        CHECKS.append(
            ("轮次 3 · edit 后的标题真正落盘", "内部速览" in head)
        )

    # ---------------- 轮次 4：HITL 删除（reject） ----------------
    section("轮次 4 · 请求删除笔记 —— 用 reject 拦下（新 thread）")
    victim = sorted(p.name for p in NOTES_DIR.glob("*.md"))[0]
    turn4 = f"删掉 {victim}，它没用了。"
    print(f"用户：{turn4}")
    result4 = run_turn(agent, {"configurable": {"thread_id": str(uuid4())}}, user, turn4)
    payload4 = interrupt_of(result4)
    if payload4 is None:
        CHECKS.append(("轮次 4 · 删除触发中断", False))
        print("  ✗ 没有触发中断")
    else:
        request = payload4["action_requests"][0]
        print(f"  中断动作：{request['name']}")
        reason = "这是知识库的原始资料，不要删除。请改为在回复里说明它的用途。"
        print(f"  决策 reject：{reason}")
        result4 = resume(agent, config2, user, [{"type": "reject", "message": reason}])
        still_there = (NOTES_DIR / victim).is_file()
        print(f"  拒绝后文件还在吗：{still_there}")
        CHECKS.append(("轮次 4 · reject 后文件未被删除", still_there))

    # ---------------- 汇总 ----------------
    section("核对结果")
    for label, ok in CHECKS:
        print(f"  {'✓' if ok else '✗'} {label}")
    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} 通过")

    leaked = sweep(before, NOTES_DIR, PUBLISHED_DIR)
    print(f"已清理演示产物 {len(leaked)} 个：{[p.name for p in leaked]}")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())

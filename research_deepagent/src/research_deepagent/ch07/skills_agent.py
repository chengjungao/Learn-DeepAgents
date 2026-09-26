"""ch07 · 把 SKILL.md 挂进 Agent（最小可用）。

三条容易记错的规则
------------------
1. ``skills`` 收的是**父目录**，不是 SKILL.md 本身：
   ``skills=["/skills/"]`` 对应 ``/skills/<name>/SKILL.md``。
   传 ``["/skills/myskill.md"]`` 不符合目录扫描约定，会被静默忽略。
2. 路径是**相对 backend 根目录**的虚拟路径，不是文件系统路径。
3. ``skills`` 参数不会替你创建目录或文件——目录得先真实存在。

Progressive Disclosure 三级加载（ch07）
-------------------------------------
L1 元数据（name + description + 路径）→ 启动时进系统提示词
L2 指令正文 → Agent 判断相关后自己调 ``read_file`` 读
L3 资源（references/ assets/ scripts/）→ 指令引用到时才读

所以 description 是**唯一**的路由依据：写得含糊就会漏召回或误召回。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend

# ch07 目录本身就是 backend 根：里面只有一个 skills/ 子树
CH07_ROOT = Path(__file__).resolve().parent
SKILLS_MOUNT = "/skills/"

SKILL_NAME = "learn-note-style"

SYSTEM_PROMPT = """You are a documentation assistant for a DeepAgents learning repo.

When the user asks you to write or tidy up a learning note, consult the
available skills before drafting, then follow the format the skill prescribes.
"""


def _default_model() -> Any:
    """复用主项目已配置好的模型，避免重复维护 env→provider 分支。"""
    from research_deepagent.agent import model as project_model

    return project_model


def build_agent(model: Any | None = None):
    """挂载 /skills/ 的 Agent。

    ``FilesystemBackend(root_dir=CH07_ROOT)`` 让内置文件工具能真的读到
    磁盘上的 SKILL.md —— 默认的 StateBackend 是图内虚拟 FS，扫不到任何技能。
    """
    return create_deep_agent(
        model=model or _default_model(),
        backend=FilesystemBackend(root_dir=CH07_ROOT, virtual_mode=True),
        skills=[SKILLS_MOUNT],
        system_prompt=SYSTEM_PROMPT,
    )

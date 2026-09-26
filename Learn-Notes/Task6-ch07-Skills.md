# Task6 · Skills：写一个最小可用的 SKILL.md

环境：Windows + `research_deepagent`，deepagents 0.7.13 / langchain 1.4.0。
代码在 `research_deepagent/src/research_deepagent/ch07/`，
探测脚本 `_ch07_skills_probe.py`、`_ch07_live_run.py`。

要求里的「体验并行任务、追加指令、取消或恢复」是 ch06 的内容，实操记录在 **Task5 笔记**里；
这篇只讲 ch07 的 Skills——按 Agent Skills 规范写了一个最小可用的 `SKILL.md`，
实测它被按需加载、并且真的影响了模型输出。

## 一、最重要的知识

**目录约定**：`skills=["/skills/"]` 收的是**父目录**，对应 `/skills/<name>/SKILL.md`；
路径是**相对 backend 根目录**的虚拟路径。传 `["/skills/myskill.md"]` 不符合扫描约定，
会被静默忽略——不报错，也不生效。

**frontmatter 只有两个必填**：`name`（必须与父目录同名）和 `description`。
其余字段（版本、作者、依赖）都是可选的。

**Progressive Disclosure 三级加载，是 Skills 最关键的设计**：

| 层级 | 内容 | 时机 |
|---|---|---|
| L1 | name + description + 路径 | Agent 启动时就进系统提示词 |
| L2 | SKILL.md 正文 | Agent 判断相关后自己 `read_file` |
| L3 | `references/` `assets/` `scripts/` | 正文引用到时才读 |

所以 `description` 是**唯一**的路由依据——Agent 不提前读正文，只能靠这一句话决定要不要用。
写得含糊会同时导致漏召回（该用没用）与误召回（不该用乱用）。

**Skills / Memory / Tools 的分工**：所有对话都要的 → Memory；特定任务才要的专业指令 → Skills；
要执行的原子操作 → Tools。三者的区别在于**加载时机**，不在于内容形态。

## 二、实践结果

### 挂载方式

```python
CH07_ROOT = Path(__file__).resolve().parent          # .../ch07

graph = create_deep_agent(
    model=model,
    backend=FilesystemBackend(root_dir=CH07_ROOT, virtual_mode=True),
    skills=["/skills/"],                              # 父目录，不是 SKILL.md 本身
    system_prompt=SYSTEM_PROMPT,
)
```

技能目录就放在代码旁边：`ch07/skills/learn-note-style/SKILL.md`。
backend 指向 `ch07/`，所以虚拟路径 `/skills/` 正好对上真实目录。

写的技能是 `learn-note-style`——把本项目的笔记规范（四类内容、四节封顶、交付前自检）
固化成一个 SKILL.md。选它当样例是因为规范我熟，出问题一眼能看出是加载没生效还是内容不对。

### 三级加载实测（6/6 通过）

探测的关键设计：**在 description（L1）和正文（L2）各放一个独有的标记串**，
然后让假模型记录每次收到的 SystemMessage，按标记判断哪一层进了提示词。

| 场景 | 系统提示词长度 | L1 标记 | L2 标记 |
|---|---|---|---|
| `skills=["/skills/"]` | **2195 字符** | ✓ 在 | ✓ 不在 |
| 无 `skills=`（对照） | **5 字符** | ✗ | ✗ |

L2 标记后来出现在 `read_file` 的返回里，说明正文是**按需**加载的，不是启动时预载。
对照组只有 5 字符，说明没挂 skills 时提示词里确实什么都没有。

### 真实模型实跑

实测两次（24.8s / 29.1s），工具轨迹一致：

```
  → read_file(/skills/learn-note-style/SKILL.md)   # 自己去读的
  → write_file(/notes/批量脚本化假模型做AB对比.md)   # 按骨架落盘
```

两次都从 description 判断出技能相关、主动读正文、按骨架起草，并在回复里**复述技能的自检项**
（「四节封顶」「H2 恰好 4 个」「150-250 行」「代码围栏成对」「去掉了 AI 腔措辞」）。
技能里的规范真的落地到了输出，不是躺在磁盘上自我欣赏。

### 复现

```bash
cd research_deepagent
.venv/Scripts/python.exe ../_ch07_skills_probe.py   # 三级加载，不耗 token
.venv/Scripts/python.exe ../_ch07_live_run.py       # 真实模型
```

## 三、遇到的问题

### 1. 项目装了 2 个 Skill，但 Agent 看不到

`skills-lock.json` 里登记了 `langchain-dev-guide` 与 `langsmith-trace`，
文件也真的躺在 `.agents/skills/` 与 `agent/skills/` 下，但
`src/research_deepagent/agent.py` 里**没有任何 `skills=` 参数**——搜索整个源码目录，`skills` 零命中。

先在 `agentseek_api` 里确认过它不会自动注入：该包里唯一的 `skills` 出现在
`a2a_server.py` 的 AgentCard，那是 A2A 协议的「技能」字段，与 SKILL.md 无关。
所以这两个技能目前是**死重量**：占了磁盘，不进提示词，不被发现。

按 ch07 的规则，修复要三处一起动（**尚未落地，改动会改变产线 Agent 的提示词与路由，先不动**）：

```python
# 1. backend 增加一条路由，指向技能目录
def _build_backend() -> CompositeBackend:
    return CompositeBackend(
        default=StateBackend(),
        routes={
            NOTES_MOUNT: FilesystemBackend(root_dir=NOTES_ROOT, virtual_mode=True),
            "/skills/": FilesystemBackend(
                root_dir=Path(__file__).resolve().parents[2] / ".agents" / "skills",
                virtual_mode=True,
            ),
        },
    )

# 2. create_deep_agent 增加 skills 参数
graph = create_deep_agent(..., skills=["/skills/"])

# 3. 注意挂载前缀会替换路径语义，技能目录下应直接是 <name>/SKILL.md
```

### 2. `GraphRecursionError`：实例字段 + `model_copy`

ch07 探测第一版写了 `cursor: int` 作为 pydantic 字段，`bind_tools` 每次返回新分身，
自增留在分身上，原实例永远读到 0 → 模型无限重复 `read_file` → 递归到 30 层报错。
改放模块级变量即可，与 ch05 的修法一致（见 Task5 笔记问题 2）。

### 3. 模型的 `write_file` 落进源码目录，还留一串空目录

`FilesystemBackend(root_dir=CH07_ROOT)` 是为了让内置文件工具真能读到 `skills/`，
代价是模型一调 `write_file`，产物就直接落在 `src/research_deepagent/ch07/` 里——
第一次跑完冒出个「批量脚本化假模型做AB对比.md」，内容正是 SKILL.md 的骨架
（反过来说，这也算技能生效的副产品证据）。写嵌套路径（`/home/user/notes/x.md`）时更隐蔽：
只删文件的话，会剩下一条空目录链。

探针脚本是一次性用品，源码目录不能被它污染。跑前拍快照、跑后取差集删文件，删完逐层往上收空目录
（收到 `CH07_ROOT` 就停，源码目录本身不能删）：

```python
for p in leaked:                      # leaked = 跑后快照 - 跑前快照
    p.unlink()
    parent = p.parent
    while parent != CH07_ROOT and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent
```

改完复跑，`ls -R` 只剩 `__init__.py`、`skills/learn-note-style/SKILL.md`、`skills_agent.py`。

## 四、收获

**「不可见」和「不存在」要分开判断。** 两个技能在磁盘上、在 lock 文件里都齐全，
唯一的缺口是一个没传的参数。查这类问题时，看文件在不在只算半步，
还得确认有没有代码真的把它读进去——`grep skills src/` 零命中，比任何推断都直接。

**技能的价值在 description 那一句话上。** 正文写得多完整，Agent 看不见也是白搭；
它只凭 description 决定要不要点开。这次实测里模型每次都命中，靠的就是描述里
那句「当需要为 DeepAgents 课程写学习笔记时使用」——把**使用场景**写在最前面，
而不是把技能内容概括一遍。

# Task6 · 异步子 Agent 与 Skills

环境：Windows + `research_deepagent`，deepagents 0.7.13 / agentseek-api 0.2.3（Agent Protocol 服务）。
代码在 `research_deepagent/src/research_deepagent/ch06_demo/` 与 `.../ch07/`，
探测脚本 `_ch06_async_probe.py`、`_ch06_async_live.py`、`_ch07_skills_probe.py`、`_ch07_live_run.py`。

这一节在真实 Agent Protocol 服务上跑通了异步子 Agent 的四件事：并行启动、查进度、追加指令、取消；
并按 Agent Skills 规范写了一个最小可用的 `SKILL.md`，实测它被按需加载且真正影响了输出。
一处踩坑值得单独说：**`url=None`（ASGI 同部署）在本环境走不通**，必须显式填 `url`。

## 一、最重要的知识

**同步 vs 异步的判定法则**：子任务 5 秒内能完成用同步；可能跑几分钟、且过程要可交互，上异步。
同步的 `task()` 会把主 Agent 一起阻塞；异步的 `start_async_task` 立刻返回 task id，主 Agent 回到对话循环。

**五把遥控器由 `AsyncSubAgentMiddleware` 自动注入**：

| 工具 | 作用 |
|---|---|
| `start_async_task` | 启动后台任务，立刻返回 task id |
| `check_async_task` | 查状态与结果 |
| `update_async_task` | 给运行中的任务追加指令 |
| `cancel_async_task` | 终止任务 |
| `list_async_tasks` | 列出所有任务 |

**任务元数据放独立 state 通道 `async_tasks`，不塞消息历史。** 原因是上下文逼近上限时会自动摘要压缩，
task id 只活在 ToolMessage 里就会丢。放进 channel 之后，压缩了也能用 `list_async_tasks` 找回来。
这和虚拟文件系统、TodoList 是同一套思路：**会被截断的放消息历史，必须长存的进 state channel。**

**`update_async_task` 在同一条 thread 上以 interrupt 策略重启**，所以 task id **保持不变**——
实测确认，与文档一致。

**Skills 的目录约定**：`skills=["/skills/"]` 收的是**父目录**，对应 `/skills/<name>/SKILL.md`；
路径是**相对 backend 根目录**的虚拟路径。传 `["/skills/myskill.md"]` 不符合扫描约定，会被静默忽略。

**frontmatter 只有两个必填**：`name`（必须与父目录同名）和 `description`。

**Progressive Disclosure 三级加载**，是 Skills 最关键的设计：

| 层级 | 内容 | 时机 |
|---|---|---|
| L1 | name + description + 路径 | Agent 启动时就进系统提示词 |
| L2 | SKILL.md 正文 | Agent 判断相关后自己 `read_file` |
| L3 | references/ assets/ scripts/ | 正文引用到时才读 |

所以 `description` 是**唯一**的路由依据——Agent 不提前读正文。写得含糊会同时导致漏召回与误召回。

**Skills / Memory / Tools 的分工**：所有对话都要的 → Memory；特定任务才要的专业指令 → Skills；
要执行的原子操作 → Tools。

## 二、实践结果

### 异步子 Agent：不启服务也能验的注入事实（5/5 通过）

| 检查项 | 结果 |
|---|---|
| 绑定工具数 | 8 → **13**（+5 把遥控器） |
| 5 把遥控器 | 全部注入 |
| `async_tasks` 通道 | 声明后出现，不声明时不存在 |
| `task` 工具 | 仍在（同步路径保留） |

### 异步子 Agent：真实服务上的完整生命周期

服务用**独立配置**启动，没有改动项目原有的 `langgraph.json`：

```bash
cd research_deepagent
.venv/Scripts/agentseek-api.exe dev -c ch06_demo/langgraph.json --port 2025 --no-browser --no-reload
```

四个阶段实测：

```
① 并行启动两个后台任务
   返回耗时 16.4s
   此刻 async_tasks 通道（2 条）：
     · agent=researcher status=running task_id=adeb782c-...
     · agent=researcher status=running task_id=fb8a4339-...
   → ✓ 返回时任务仍在 running = 非阻塞

② 查进度
   直接读通道：两条仍是 running（通道状态只在 check 时刷新）
   让 supervisor 去 check（32.6s）后：两条都 status=success
   地面对照（直接查子 Agent 线程 run 状态）：run_status=success ✓

③ 追加指令（10.8s）
   supervisor：该任务会被中断并在同一线程上重新运行，task_id 保持不变
   → task_id 与启动时完全一致 ✓

④ 取消（9.2s）
   取消后再读通道：status=running → status=cancelled ✓
```

并行是真的并行：两个任务各自拿到独立 thread，返回时都还在跑。
16.4s 不是子 Agent 的耗时（它只 sleep 6s），是 supervisor 自己那一轮 LLM 的延迟。

### Skills：三级加载实测（6/6 通过）

| 场景 | 系统提示词长度 | L1（description） | L2（正文） |
|---|---|---|---|
| `skills=["/skills/"]` | **2195 字符** | ✓ 在 | ✓ 不在 |
| 无 `skills=`（对照） | **5 字符** | ✗ | ✗ |

L2 标记在 `read_file` 的返回里出现，说明正文是**按需**加载的，不是启动时预载。

### Skills：真实模型实跑

实测两次（24.8s / 29.1s），工具轨迹一致：

```
  → read_file(/skills/learn-note-style/SKILL.md)   # 自己去读的
  → write_file(/notes/批量脚本化假模型做AB对比.md)   # 按骨架落盘
```

两次都从 description 判断出技能相关、主动读正文、按骨架起草，并在回复里**复述技能的自检项**
（「四节封顶」「H2 恰好 4 个」「150-250 行」「代码围栏成对」「去掉了 AI 腔措辞」）。
技能里的规范真的落地到了输出，不是躺在磁盘上自我欣赏。

复现：

```bash
cd research_deepagent
.venv/Scripts/python.exe ../_ch06_async_probe.py    # 注入事实
.venv/Scripts/python.exe ../_ch07_skills_probe.py   # 三级加载
# 需要服务的两个：
.venv/Scripts/python.exe ../_ch06_async_live.py     # 先起 2025 服务
.venv/Scripts/python.exe ../_ch07_live_run.py       # 真实模型
```

## 三、遇到的问题

### 1. `url=None`（ASGI 同部署）在本环境走不通

第一次跑端到端，`start_async_task` 直接失败：

```
Failed to launch async subagent 'researcher': 'NoneType' object is not callable
```

根因在 `deepagents/middleware/async_subagents.py`：

- `start_async_task` 同时注册了同步 `func` 和异步 `coroutine`
- **同步分支**走 `_ClientCache.get_sync()`，该方法 `url is None` 时直接 `raise ValueError`
  （原文：「ASGI transport (url=None) requires async invocation」）
- **异步分支**走 `get_async()` → `get_client(url=None)`，需要一个进程内 ASGI app 引用，
  agentseek-api 没有暴露 → 在调用处炸成 `'NoneType' object is not callable`

修法：**显式填 `url`**，走 HTTP 传输：

```python
AsyncSubAgent(
    name="researcher",
    description="...",
    graph_id="async-researcher",
    url="http://127.0.0.1:2025",   # ← 补上这一行就好了
)
```

这正好对应 ch06 说的「拆分部署 / 混合」拓扑——同部署的 ASGI 那条路在本环境不可用，
指向自身走 HTTP 就可以。

### 2. `async_tasks` 是 dict，不是 list

中间件用 `Command(update={"async_tasks": {task_id: task}})` 更新，通道是 `task_id → task` 的映射。
我第一版按 list 迭代，拿到一堆字符串键，再 `.get()` 就报
`'str' object has no attribute 'get'`。

### 3. 通道状态只在 `check_async_task` 时才刷新

绕过 Agent 直接读 `async_tasks`，两条任务在子 Agent 早就跑完之后仍然显示 `running`。
不是 bug——`check` 才会去服务端拉状态并回写通道。
所以「报告进度」这件事必须经过 Agent 的 `check_async_task`，直接读通道拿到的是上次检查的旧值。

### 4. 项目装了 2 个 Skill，但 Agent 看不到

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

### 5. `GraphRecursionError`：实例字段 + `model_copy`

ch07 探测第一版写了 `cursor: int` 作为 pydantic 字段，`bind_tools` 每次返回新分身，
自增留在分身上，原实例永远读到 0 → 模型无限重复 `read_file` → 递归到 30 层报错。
改放模块级变量即可，与 ch05 的修法一致。

### 6. 模型的 `write_file` 落进源码目录，还留一串空目录

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

**「同部署零配置」是有代价的承诺。** ch06 说 ASGI 传输零网络延迟、零鉴权，是推荐起手式；
实际在这个 Agent Protocol 服务上它需要宿主提供一个进程内 ASGI app 引用，而宿主没给。
多填一行 `url` 就从「起不来」变成「全流程跑通」——遇到框架的推荐路径不工作时，
先去看它依赖了宿主什么隐含约定，比换方案快。

**「不可见」和「不存在」要分开判断。** 两个技能在磁盘上、在 lock 文件里都齐全，
唯一的缺口是一个没传的参数。查这类问题时，看文件在不在只算半步，
还得确认有没有代码真的把它读进去——`grep skills src/` 零命中，比任何推断都直接。

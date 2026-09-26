# Task5 · 子 Agent：职责拆分、上下文隔离与异步调度

环境：Windows + `research_deepagent`，deepagents 0.7.13 / langchain 1.4.0 / agentseek-api 0.2.3。
代码在 `research_deepagent/src/research_deepagent/ch05/` 与 `.../ch06_demo/`，
探测脚本 `_ch05_isolation_probe.py`、`_ch05_live_run.py`、`_ch06_async_probe.py`、`_ch06_async_live.py`。

ch05 把子 Agent 拆成两个职责互斥的角色，用脚本化假模型实测隔离边界，再用真实模型跑了一次；
ch06 在真实服务上跑通异步子 Agent 的并行启动、查进度、追加指令、取消（即要求里那句
「体验并行任务、追加指令、取消或恢复」）。一处踩坑值得单独说：**`url=None` 在本环境走不通**。

## 一、最重要的知识

### 上下文隔离（ch05）

**隔离的是消息历史，不是文件。** 子 Agent 在独立上下文里执行，中间的工具调用不进父上下文，
父 Agent 只拿到子 Agent 的最后一条消息。两者共用同一个 backend，所以文件是共享的——
隔离的是「对话」，不是「磁盘」。

**子 Agent 定义只有 3 个必填字段**：`name` / `description` / `system_prompt`。

**继承规则有个反直觉的地方**：

| 字段 | 继承主 Agent？ |
|---|---|
| `system_prompt` | 不继承，必须自己写 |
| `middleware` | 不继承 |
| `skills` | 不继承（只有 general-purpose 例外） |
| `tools` | 不写=继承全部；**写了=完全替换，不合并** |

**`tools` 的「完全替换」有个边界**：它替换的是传进 `create_deep_agent(tools=)` 的那批工具，
中间件提供的工具不受影响。实测 archivist 显式只给 2 个自定义工具，最终绑定 9 个——
7 个是 `FilesystemMiddleware` 注入的内置文件工具（`ls`/`read_file`/`glob`/…），一个没少。
想让子 Agent 连文件工具都没有，得动中间件，不是动 `tools=`。

**`task` 工具是单向的**：主 Agent 由 `SubAgentMiddleware` 自动注入 `task`，
子 Agent 拿不到 `task`——否则可以无限套娃。这点是实测确认的，不是照文档抄的。

**`description` 是唯一的路由依据。** 主 Agent 看不到子 Agent 的 system_prompt，
只靠 description 决定派给谁，所以要写成「什么时候用它」，而不是「它是什么」。

**general-purpose 是唯一的例外**：它继承主 Agent 的 system_prompt / tools / model / skills。
v0.7 还多一条边界——主 Agent 显式传入的 `TodoListMiddleware` 会被 general-purpose 继承，
但 `subagents=[...]` 声明的专业子 Agent **不继承**，要各自在 `middleware` 里开。
继承的是「规划能力」，不是主 Agent 已经生成的那份清单，每个 Agent 维护自己的 todos。

**想彻底关掉子 Agent 机制，别去动中间件。** 用 `excluded_middleware` 排除 `SubAgentMiddleware`
会直接抛 `ValueError`。正确做法是注册一个 `HarnessProfile`，
把 `general_purpose_subagent` 设为 `GeneralPurposeSubagentProfile(enabled=False)`，
再让 `subagents=[]`——两步缺一不可。

**子 Agent 不受 `recursion_limit` 约束。** 它由 `create_agent` 构建时自带绑定 9999，
外层 `.with_config()` 覆盖无效——`subagents.py` 的 `ensure_config` 注释写明
「the subagent's bound config still wins collisions」。要管子 Agent 只能在它自己的
`middleware` 里放 `ToolCallLimitMiddleware`，上面两个子 Agent 各带了 `run_limit=8` 的硬停。

### 同步 vs 异步（ch06）

**判定法则**：子任务 5 秒内能完成用同步，可能跑几分钟、且过程要可交互就上异步——
同步的 `task()` 会把主 Agent 一起阻塞，异步的 `start_async_task` 立刻返回 task id。

**五把遥控器由 `AsyncSubAgentMiddleware` 自动注入**：

| 工具 | 作用 |
|---|---|
| `start_async_task` | 启动后台任务，立刻返回 task id |
| `check_async_task` | 查状态与结果 |
| `update_async_task` | 给运行中的任务追加指令 |
| `cancel_async_task` | 终止任务 |
| `list_async_tasks` | 列出所有任务 |

**任务元数据放独立 state 通道 `async_tasks`，不塞消息历史。** 上下文逼近上限时会自动摘要压缩，
task id 只活在 ToolMessage 里就会丢；放进 channel 之后，压缩了也能用 `list_async_tasks` 找回来。
这和虚拟文件系统、TodoList 是同一套思路：**会被截断的放消息历史，必须长存的进 state channel。**

**`update_async_task` 在同一条 thread 上以 interrupt 策略重启**，所以 task id 保持不变。

## 二、实践结果

两个子 Agent 按「数据源 + 工具集 + 输出契约」三件套拆开，三者同时不同才算真拆：

| 子 Agent | 数据源 | 工具集 | 输出契约 |
|---|---|---|---|
| `note-archivist` | 本地笔记 | `search_local_notes` / `read_local_note` | 带路径+行号的引用清单，≤300 词 |
| `web-scout` | 开放网络 | `tavily_search` / `think_tool` | 带 URL 的发现清单，≤500 词 |

### 隔离探测：10/10 通过

用两组标记做判定——中间过程标记写在**子 Agent 内部工具调用的参数**里（真实执行，
`search_local_notes` 会真的去扫笔记目录），终稿标记写在**子 Agent 最后一条消息**里。

模型调用序列（按发生顺序）：

```
1. parent          cursor=0 bound=8 msgs=2
2. note-archivist  cursor=0 bound=9 msgs=2
3. note-archivist  cursor=1 bound=9 msgs=4
4. parent          cursor=1 bound=8 msgs=4
```

父上下文一共 4 条消息：

```
HumanMessage: 研究一下 X
AIMessage(tool_calls=['task']):
ToolMessage: ARCHIVIST_FINAL_MARKER 命中 3 个笔记，详见路径与行号。
AIMessage: PARENT_FINAL_MARKER 已整合子 Agent 的结论。
```

子 Agent 内部那次 `search_local_notes` 带着 `VERBOSE_MARKER_ARCHIVIST` 的参数，
**没有出现在父上下文任何位置**；而 `ARCHIVIST_FINAL_MARKER` 出现在 `ToolMessage` 里。
子 Agent 跑了 2 次模型调用（发工具 → 收结果后给终稿），父 Agent 只看到这 2 次的结果。

工具集绑定实测：

```
parent         (8 个)：delete, edit_file, glob, grep, ls, read_file, task, write_file
note-archivist (9 个)：delete, edit_file, glob, grep, ls, read_file,
                       read_local_note, search_local_notes, write_file
```

archivist 里没有 `tavily_search`、没有 `task`；scout 里没有本地笔记工具。
两边的自定义工具互斥，内置文件工具都在——正是上面那条边界。

### 真实模型实跑

```
耗时 34.6s
  → task(subagent_type='note-archivist')
  → task(subagent_type='note-archivist')
父上下文消息数：5
```

两次委派都路由到 `note-archivist`（问的是「我的本地笔记里关于 write_todos 记了什么」），
返回内容带文件路径与行号，如 `research_deepagent/frontend/public/work/deep-agents-write-todos.md L9`。
父上下文只有 5 条消息，子 Agent 的检索过程一条没漏进来。

### 异步：不启服务也能验的注入事实（5/5 通过）

| 检查项 | 结果 |
|---|---|
| 绑定工具数 | 8 → **13**（+5 把遥控器） |
| 5 把遥控器 | 全部注入 |
| `async_tasks` 通道 | 声明后出现，不声明时不存在 |
| `task` 工具 | 仍在（同步路径保留） |

### 异步：真实服务上的完整生命周期

服务用**独立配置**启动，没有改动项目原有的 `langgraph.json`：

```bash
cd research_deepagent
.venv/Scripts/agentseek-api.exe dev -c ch06_demo/langgraph.json --port 2025 --no-browser --no-reload
```

四个阶段实测：

```
① 并行启动两个后台任务（返回耗时 16.4s）
   此刻 async_tasks 通道 2 条，都是 status=running（adeb782c-... / fb8a4339-...）
   → ✓ 返回时任务仍在 running = 非阻塞

② 查进度
   直接读通道：仍是 running（通道状态只在 check 时刷新）
   让 supervisor 去 check（32.6s）后：两条都 success，地面对照 run_status=success ✓

③ 追加指令（10.8s）：该任务被中断并在同一线程重跑，task_id 与启动时完全一致 ✓

④ 取消（9.2s）：status=running → status=cancelled ✓
```

并行是真的并行：两个任务各自拿到独立 thread，返回时都还在跑。
16.4s 不是子 Agent 的耗时（它只 sleep 6s），是 supervisor 自己那一轮 LLM 的延迟。

复现：

```bash
cd research_deepagent
.venv/Scripts/python.exe ../_ch05_isolation_probe.py   # 机制，不联网不耗 token
.venv/Scripts/python.exe ../_ch05_live_run.py          # 真实模型（ch05）
.venv/Scripts/python.exe ../_ch06_async_probe.py       # 异步注入事实
.venv/Scripts/python.exe ../_ch06_async_live.py        # 需先起 2025 服务
```

## 三、遇到的问题

### 1. 隔离检查「假通过」——角色键不匹配导致子 Agent 空跑

第一版探测返回 `[archivist script exhausted]`，但**隔离检查全部通过**。

根因：`_role_of()` 按工具名返回短名 `"archivist"`，而剧本字典的键是 `"note-archivist"`。
`scripts.get("archivist")` 拿到 `None` → 子 Agent 空剧本 → 立刻返回哨兵值收尾。
子 Agent 压根没执行，中间过程当然不污染父上下文——**检查通过是因为什么都没发生**。

定位靠把「每次 `_generate` 的 role / cursor / 剧本长度」打出来：

```
[dbg] _generate role=archivist cursor=0 script_len=0 msgs=2   ← script_len=0 露馅
```

修法两条：角色名与剧本键**逐字对齐**；剧本为空时 `raise RuntimeError`，不要静默返回哨兵值。

### 2. 假模型的可变状态不能放 pydantic 实例字段

`bind_tools` 返回的 `model_copy` 分身各持一份 `cursor`，`int` 自增留在分身上，
原实例永远读 0。ch07 探测里这个坑直接表现为 `GraphRecursionError`（无限重复 `read_file`）。
修法：这类「要跨调用累加 / 要事后读取」的状态一律放**模块级变量**。

### 3. `url=None`（ASGI 同部署）在本环境走不通

第一次跑端到端，`start_async_task` 直接失败：

```
Failed to launch async subagent 'researcher': 'NoneType' object is not callable
```

根因在 `deepagents/middleware/async_subagents.py`：同步分支走 `_ClientCache.get_sync()`，
该方法 `url is None` 时直接 `raise ValueError`（原文：「ASGI transport (url=None) requires
async invocation」）；异步分支走 `get_client(url=None)`，需要一个进程内 ASGI app 引用，
agentseek-api 没有暴露 → 在调用处炸成 `'NoneType' object is not callable`。

修法：**显式填 `url`**，走 HTTP 传输：

```python
AsyncSubAgent(name="researcher", description="...", graph_id="async-researcher",
              url="http://127.0.0.1:2025")   # ← 补上 url 就好
```

这正好对应 ch06 说的「拆分部署 / 混合」拓扑——同部署的 ASGI 那条路在本环境不可用，
指向自身走 HTTP 就可以。

### 4. `async_tasks` 是 dict，不是 list

中间件用 `Command(update={"async_tasks": {task_id: task}})` 更新，通道是 `task_id → task` 的映射。
第一版按 list 迭代，拿到一堆字符串键，再 `.get()` 就报 `'str' object has no attribute 'get'`。

### 5. 通道状态只在 `check_async_task` 时才刷新

绕过 Agent 直接读 `async_tasks`，两条任务在子 Agent 早就跑完之后仍然显示 `running`。
不是 bug——`check` 才会去服务端拉状态并回写通道。
所以「报告进度」必须经过 Agent 的 `check_async_task`，直接读通道拿到的是上次检查的旧值。

## 四、收获

**假通过比失败更危险。** 失败会报错、会被看见；假通过会告诉你「10/10 通过」，
让你把一条根本没验证的结论写进笔记。要防它，就得让「前提不成立」这件事本身炸出来——
空剧本直接抛异常，是一行代码的代价。

**「同部署零配置」是有代价的承诺。** ch06 说 ASGI 传输零延迟零鉴权，是推荐起手式；
实际它需要宿主提供一个进程内 ASGI app 引用，而 agentseek-api 没给。多填一行 `url`
就从「起不来」变成「全流程跑通」——框架的推荐路径不工作时，先看它依赖了宿主什么隐含约定。

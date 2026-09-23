# Task4 · 任务规划：write_todos 与小型文档 Agent

> 2026-09-23 · Windows / Python 3.13.14 / deepagents 0.7.13 / GLM-5.2


## 一、知识

### v0.7 不再默认装 TodoListMiddleware

本章影响最大的一条。不显式传 `middleware=[TodoListMiddleware()]`，主 Agent 手里就没有 `write_todos`。

实测证据（脚本化假模型读走「本次绑给模型的工具清单」）：

| 配置 | 绑给模型的工具 | 图节点 |
|---|---|---|
| 不传 `TodoListMiddleware`（项目原状态） | 8 个：`delete, edit_file, glob, grep, ls, read_file, task, write_file` | 无 Todo 节点 |
| 显式传 `TodoListMiddleware()` | 9 个：多一个 **`write_todos`** | 多一个 `TodoListMiddleware.after_model` |

不传的情况下硬发一次 `write_todos`：

```
Error: write_todos is not a valid tool, try one of [ls, read_file, write_file, edit_file, delete, glob, grep, task]
```

deepagents 自己的 Codex harness profile 里写着原因（`deepagents/profiles/harness/_openai_codex.py:73`）：

> Includes `TodoListMiddleware` … since the Codex system prompt references reconciling TODO/plan items via `write_todos`; **the SDK no longer provides it by default.**

### 中间件按配置方式分三类

`create_deep_agent()` 的本质就是把一组中间件组装到 Agent 上。按**怎么加**分三类（不是三个执行层级）：

| 类别 | 加的方式 | 例子 |
|---|---|---|
| 默认中间件 | 自动 | `FilesystemMiddleware`、`SummarizationMiddleware`、`PatchToolCallsMiddleware` |
| 专用参数 | `skills=` / `memory=` / `subagents=` / `interrupt_on=` | 对应注入 Skills / Memory / 子代理 / 人工审批 |
| `middleware=[...]` | 显式传入 | `TodoListMiddleware`、`ToolCallLimitMiddleware`、`PIIMiddleware` |

同名实例会**原位置换**默认实例，而且是整实例替换，不按字段合并。

手动配齐同一套能力的对比（`ch04/langchain_layer.py`，两边都用假模型）：

```
create_deep_agent(middleware=[Todo])
  → 9 个工具，节点含 PatchToolCallsMiddleware.before_agent + TodoListMiddleware.after_model
create_agent(middleware=[Todo, Filesystem])
  → 8 个工具，节点少 PatchToolCalls，少了 task（没有子代理）
```

`create_deep_agent` 替我省掉的是 `FilesystemMiddleware`、`SummarizationMiddleware`、`PatchToolCallsMiddleware` 和子代理；**Todo 两边都得自己加**。

### todos 存在 state 里，不占消息历史

`TodoListMiddleware` 同时注入三样东西：`write_todos` 工具、`todos` 状态字段、规划提示词。

关键点是 `todos` 与 `messages` **分开管理**：默认的对话总结压缩发给模型的消息，但不动 `todos` 字段。所以长任务跑到第 6 步、前面历史被摘要压掉之后，Agent 仍能靠清单知道自己走到哪了——这是任务清单与上下文管理的协同点。

每条 todo 只有两个字段，`status` 三态：`pending` → `in_progress` → `completed`。

跨次调用要接续清单，需要 Checkpointer + 同一个 `thread_id`；**只加 `TodoListMiddleware` 不会自动接续**（默认没有 checkpointer）。子代理有独立中间件栈，不会读主 Agent 的清单。

### 两类 Hook 的边界

`before_agent` / `before_model` / `after_model` / `after_agent` 是 **Node-style**，编译成独立图节点；`wrap_model_call` / `wrap_tool_call` 是 **Wrap-style**，在 model/tools 节点内部包裹一次调用。

这个区别对 `interrupt()` 重要：Node-style 有清晰的节点边界，恢复时容易推断哪些逻辑会重放；Wrap-style 恢复时可能连同 `handler` 一起重跑。所以自定义人工中断应放 Node-style Hook。

### completed 只是模型的自我标记

清单全部 completed 不等于任务完成。应用要另外检查产物——报告是否生成、引用能否核实、代码是否过测试。这一条我在下面修缺陷时正好撞上了。

## 二、实战：小型文档 Agent

`src/research_deepagent/ch04/todo_doc_agent.py`，跑一次真实任务：

```bash
.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'src'); \
  from research_deepagent.ch04.todo_doc_agent import run; run('你的任务')"
```

设计上和第 2 章的 research agent 有三点不同：

- **主 Agent 自己干活，不委派子代理** —— 单 Agent 形态，`todos` 的状态流转能完整观察到，不会被 `task()` 隔在里面
- **工具集最小** —— 一个搜索 + 内置文件工具 + `write_todos`
- **`FilesystemBackend` 落盘** —— 产物真写进 `frontend/public/work/`，能用编辑器直接打开

### 实跑结果（104.3 秒）

任务：*调研 Deep Agents 的 write_todos 机制：它解决什么问题、任务清单存在哪里、跨次调用怎么接续，写成一份简要文档。*

Agent 自己拆的清单：

```
[~] 搜索 Deep Agents write_todos 机制资料，整理出它解决什么问题
[ ] 搜索任务清单存储位置与跨次调用接续机制
[ ] 把关键事实写入 /work/notes.md
[ ] 整理成最终文档写入 /work/deep-agents-write-todos.md
```

`stream(stream_mode="values")` 抓到 **5 次** todo 变更，每一步做完立刻标 `completed`，没有攒着一起标：

```
第 1 版  4 条全 pending，第 1 条 in_progress
第 2 版  [x] 第1条  [~] 第2条
第 3 版  [x] 第1条  [x] 第2条  [~] 第3条
第 4 版  [x] 前3条  [~] 第4条
第 5 版  4 条全 completed
```

产物落盘：

```
frontend/public/work/notes.md                      5029 bytes  （中间笔记）
frontend/public/work/deep-agents-write-todos.md    7190 bytes  （最终文档）
```

最终文档 14 条引用，内容比我的提示词更有料——它自己挖出了「工具定义内嵌 140–180 行描述」「原理是整体覆盖而非增量更新」「灵感来自 Claude Code 的 TodoWrite」，还标了出处。这几条我没写进提示词，是它检索来的。

## 三、问题

跑通的过程暴露了两个真缺陷，都出在**项目原有代码**里，不是 ch04 新写的。

### 1. 提示词命令 Agent 调一个不存在的工具

`prompts.py` 的 `RESEARCH_WORKFLOW_INSTRUCTIONS` 第 1 步：

> 1. **Plan first**: Before using task() or write_file(), you MUST call write_todos to create a todo list …

而实测（本章第一节那张表）主 Agent 的工具里**没有** `write_todos`。一条 MUST 指令指向不存在的工具，模型只能报错或者无视。这是 ch02 加工具时没注意到的——`write_todos` 从来就不是 deepagents 默认给的。

修法：主图 `middleware` 加 `TodoListMiddleware()`。

### 2. Task3 定的 backend 修法落地

上一章（Task3）定的是 `CompositeBackend`，这轮正式改：

```python
NOTES_MOUNT = "/notes/"
NOTES_ROOT = Path(os.getenv("NOTES_ROOT") or NOTES_ROOT_DEFAULT)

def _build_backend() -> CompositeBackend:
    return CompositeBackend(
        default=StateBackend(),
        routes={NOTES_MOUNT: FilesystemBackend(root_dir=NOTES_ROOT, virtual_mode=True)},
    )
```

连带改了提示词，新增一节 `## Filesystem Layout` 说明两种路径语义：`/notes/...` 走真实磁盘可引用，其余（`/final_report.md`、`/workspace/...`）留在 graph state、线程结束即丢。子代理的工具说明里也补了两套路径的对应关系——自建的 `search_local_notes` 吃裸相对路径，内置 `read_file` 要 `/notes/` 前缀，指的是同一批文件。

### 3. 小坑：`load_dotenv()` 又按 cwd 找了

`ch04/todo_doc_agent.py` 里 `.env` 路径数错一层，`parents[2]` 指到 `src/` 而不是项目根，报 `Missing credentials`。

路径层级记一下，这个项目里已经栽过第二次：

```
src/research_deepagent/ch04/todo_doc_agent.py
  parents[0] = ch04
  parents[1] = research_deepagent   （包）
  parents[2] = src
  parents[3] = research_deepagent   （项目根，.env 在这）  ← 要的是这一层
```

### 4. 小坑：根级 `.gitignore` 管不到嵌套目录

Agent 的产物写在 `research_deepagent/frontend/public/work/`，本想忽略掉，在**根级** `.gitignore` 写了：

```gitignore
frontend/public/work/     # ✗ 没生效
```

`git check-ignore` 直接告诉你原因：

```
rc=1  research_deepagent/frontend/public/work/notes.md        (未忽略)
rc=0  frontend/public/work/notes.md
      .gitignore:55:frontend/public/work/  frontend/public/work/notes.md
```

不带前导斜杠的模式其实**哪里都能匹配**，但路径必须相对**写下这条规则的那个 `.gitignore`** 来写。根目录的规则写 `frontend/...` 只对仓库根的 `frontend/` 生效，管不到 `research_deepagent/frontend/` 这种嵌套路径。

而且 `research_deepagent/frontend/` 下**已经有自己的 `.gitignore`**（`dist/` 就在那里被忽略，见上面 rc=0 那行的输出）。正确做法是加在子目录那份：

```gitignore
# research_deepagent/frontend/.gitignore
work/
```

## 四、验证与收获

### 端到端验证：假阴性修掉了

修完直接复现 ch02 那个场景——让编排层用内置 `glob` 核实 `/notes/` 下的真实文件：

```
耗时 12.4s
--- todo 最终状态 ---
  [completed] 用 glob 列出 /notes/Learn-Notes/ 下的 markdown 文件
  [completed] 读取 Task3 文件前 3 行
  [completed] 向用户报告文件名和第一行

--- 内置工具调用轨迹 ---
  glob({"pattern": "*.md", "path": "/notes/Learn-Notes"})
      → ['/notes/Learn-Notes/Task1-AgentSeek环境搭建与踩坑.md', ... 'Task3-ch03-虚拟文件系统与后端对比.md']
  read_file({"file_path": "/notes/Learn-Notes/Task3-ch03-虚拟文件系统与后端对比.md", "limit": 3})
      → 1 # Task3 · 虚拟文件系统与两种后端的效果对比 2 3 > 2026-09-21 · …
```

三个关键点全中：

- 编排层用**内置** `glob` 就命中了磁盘上的真实文件（以前是 `No files found`）
- `read_file` 读回了正确内容
- **编排层自己用 `write_todos` 建了清单** —— ch04 的修复同时生效

这也印证了「completed 只是自我标记」那条：本次三条 todo 全 completed，产物也确实对得上。但判定依据是上面那两行工具返回，不是清单勾选——两者的区别在真出错时才会显形。

### 收获

- **`write_todos` 是 opt-in，不是默认能力**。升级 deepagents 后如果提示词里提到它，必须回头确认工具真的绑上了——直接读「本次绑给模型的工具清单」比看文档可靠。
- **提示词里的工具名要与实际绑定核对**。这次是靠脚本化假模型把工具清单打出来才发现的；换成看代码或读日志，很容易漏。
- **挂载前缀会换掉路径语义**。`CompositeBackend` 把笔记挂到 `/notes/` 后，所有引用路径都得跟着改，提示词、工具描述、子代理说明一处不能落。
- **agent 的产物质量取决于检索，不取决于提示词写多细**。我提示词里只说了「要有 Sources 段」，它自己挖出了 14 条带出处的结论。
- **`.gitignore` 的作用域按「写下规则的目录」算**。忽略嵌套路径别在根级试，先看子目录有没有自己的 `.gitignore`；`git check-ignore -v --no-index <path>` 会直接告诉你是哪条规则命中的。

### 待办

- [ ] 把 `ch04` 的两个脚本接进 AgentSeek 前端，让 `todos` 能在页面上实时渲染（`todos` 本来就是产品状态协议）
- [ ] 加 Checkpointer，验证跨次调用复用 `thread_id` 时清单真的接续（现在每次 `invoke` 都是新清单）

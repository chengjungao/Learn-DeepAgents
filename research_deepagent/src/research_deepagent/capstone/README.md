# 个人知识库助手（Capstone）

用 DeepAgents 课程的六项能力搭的一个可运行 Agent：管理用户自己的 markdown 知识库——
能拆解任务、委派子代理去检索、按规范起草对外报告、跨对话记住用户偏好，
并在**删除笔记**和**对外发布**这两个不可逆动作前要求人工审批。

不是每个能力各写一个 demo，而是**让它们在同一次请求里协同**：一条"把这批笔记整理成报告"
的指令，会依次经过规划 → 检索 → 读规范 → 起草 → 发布审批。

## 能力矩阵

| 能力 | 章节 | 在本项目里的落点 |
|---|---|---|
| 虚拟文件系统 + 多后端路由 | ch03 | `CompositeBackend` 三条路由：`/notes/` 真实磁盘、`/memories/` 长期存储、`/skills/` 技能目录 |
| 任务规划 | ch04 | `TodoListMiddleware`（v0.7 起需显式传入才有 `write_todos`） |
| 子 Agent + 上下文隔离 | ch05 | `note-librarian` 专职检索，检索过程不进父上下文 |
| Skills | ch07 | `skills/note-format/SKILL.md` 规定报告格式，Agent 判断相关后自己读取 |
| 长期记忆 | ch08 | `/memories/preferences.md` 落在 Store，跨对话保留 |
| Human-in-the-Loop | ch09 | `interrupt_on` 按风险分层，删除/发布前暂停等人决策 |

## 一次请求的完整链路

演示脚本轮次 2 的真实工具轨迹（问题：*"把知识库里关于搜索架构演进的笔记整理成一份对外发布的报告草稿"*）：

```
→ write_todos(todos=[...])                                # ch04 先规划
→ task(subagent_type=note-librarian)                      # ch05 委派检索
→ read_file(/skills/note-format/SKILL.md)                 # ch07 读格式规范
→ write_todos(todos=[...])                                # 更新清单状态
→ save_note(name=search-architecture-...-draft)           # ch03 落到真实磁盘
→ write_todos(todos=[...])
```

得到的任务清单：

```
[completed] 检索知识库中关于搜索架构演进的笔记
[completed] 读取 note-format 格式规范
[completed] 按规范起草对外发布报告草稿
[pending]   将草稿存为笔记并准备发布
```

注意 `read_file` 那一步——**没有任何代码强制它去读技能**，是 Agent 看了
`SKILL.md` 的 description（"整理成对外报告时使用"）自己判断该加载的。
产出的草稿也确实带上了技能规定的 `摘要` / `要点` / `引用` 三个小节。

## 目录结构

```
capstone/
├── knowledge_agent.py        # 主体：backend 路由、工具、子 Agent、HITL 配置、提示词
├── demo.py                   # 演示脚本（四轮，含审批交互）
├── notes/                    # 知识库：3 篇 markdown 示例笔记
├── skills/note-format/       # SKILL.md：报告格式规范
└── published/                # "对外发布"的落点（演示产物跑完会清理）
```

## 快速开始

依赖与主项目一致（deepagents 0.7.13 / langchain 1.4.0 / langgraph 1.2.11），
模型走 `research_deepagent/.env` 里的配置。

```bash
cd research_deepagent
.venv/Scripts/python.exe -m research_deepagent.capstone.demo
```

演示约 3 分钟，跑完自动清理它产生的草稿与发布文件，仓库保持干净。

也可以在自己的代码里用：

```python
from research_deepagent.capstone import build_agent, seed_memory, UserContext

store = InMemoryStore()
seed_memory(store)                      # 预置偏好文件（memory= 不会自动创建）
agent = build_agent(store=store)        # 同一个 store 复用 → 记忆跨对话

result = agent.invoke(
    {"messages": [{"role": "user", "content": "..."}]},
    context=UserContext(user_id="user-123"),
    config={"configurable": {"thread_id": "..."}},
)
```

## 演示记录

以下都是实测输出，不是示意。

### 轮次 1 · 长期记忆（新 thread）

```
用户：记住我的偏好：对外报告用简体中文写，要点控制在 5 条以内。
  → read_file(file_path=/memories/preferences.md)
  → edit_file(file_path=/memories/preferences.md,
              old_string=暂无记录.,
              new_string=- 对外报告用简体中文撰写。
                          - 报告要点控制在 5 条)
从 Store 侧核对：
  # 用户偏好
  - 对外报告用简体中文撰写。
  - 报告要点控制在 5 条以内。
```

模型走的是 `edit_file`（追加），不是 `write_file`（覆盖）——这正是提示词里要求的写入方式。

### 轮次 2 · 规划 + 子 Agent + Skills（另一个新 thread）

见上方"一次请求的完整链路"。检查项：清单 4 项、委派 1 次、草稿落盘、
草稿含技能规定的三个小节。

### 轮次 3 · HITL 发布审批（沿用轮次 2 的 thread）

```
用户：把刚才那份草稿发布出去。
  中断动作：publish_report
  参数：{'name': 'search-architecture-evolution-report-draft',
         'title': '搜索平台五代架构演进概览'}
  可选决策：['approve', 'edit', 'reject']
  决策 edit：标题改成「搜索平台五代演进：一份内部速览」
  发布目录：['search-architecture-evolution-report-draft.md']
  已发布文件首行：# 搜索平台五代演进：一份内部速览
```

改掉的标题真的落到了磁盘上的发布文件第一行。

### 轮次 4 · HITL 删除审批（新 thread）

```
用户：删掉 agent-memory-patterns.md，它没用了。
  中断动作：delete_note
  决策 reject：这是知识库的原始资料，不要删除。请改为在回复里说明它的用途。
  拒绝后文件还在吗：True
```

### 汇总

```
7/7 通过
已清理演示产物 2 个
```

## 设计取舍

**子 Agent 只给只读工具。** `note-librarian` 只有 `list_notes` / `read_note`。
"检索"这个职责本身不需要写权限，少一个写入口就少一类越权可能。
写操作留在主 Agent 手里，再叠一层 HITL——**权限靠架构收，不靠提示词求**。

**路径后缀用函数归一，不靠提示词。** 早期版本 `list_notes` 按 `*.md` 列文件，
但模型保存草稿时会掉后缀（`搜索架构报告草稿`），结果"文件写进去了，列表里看不见"。
现在所有工具走同一个 `_note_path()` 自动补 `.md`——这类静默不一致靠提示词是堵不住的。

**HITL 只按风险分层，不做一刀切。** 只读工具和内部草稿直接放行，
把审批预算留给真正不可逆的两个动作。全拦会让审批流变成形式主义。

**三条路由挂在同一个 backend 上。** `FilesystemMiddleware` 用的是同一个 backend 实例，
子 Agent 也共用它，所以路由配一次主/子 Agent 同时受益（ch03 的结论）。

## 已知限制

- 记忆用 `InMemoryStore`，进程重启即丢。生产要换 `PostgresStore`
  （`store.setup()` 建表，连接串走环境变量）。
- Checkpointer 用 `InMemorySaver`，同理；中断现场只在进程内有效。
- 演示里的"发布"是落到本地 `published/` 目录，不是真实对外渠道。
- `send_email` 这类外部副作用工具没有包含（演示用 `publish_report` 代表对外动作）。

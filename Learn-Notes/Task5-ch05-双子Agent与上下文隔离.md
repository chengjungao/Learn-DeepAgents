# Task5 · 两个职责不同的子 Agent，以及上下文隔离

环境：Windows + `research_deepagent`，deepagents 0.7.13 / langchain 1.4.0。
代码在 `research_deepagent/src/research_deepagent/ch05/`，探测脚本 `_ch05_isolation_probe.py`、`_ch05_live_run.py`。

这一节把 ch05 的子 Agent 拆成两个职责互斥的角色，用脚本化假模型实测了隔离边界，
再用真实模型跑了一次。隔离结论是「父上下文只收到终稿，收不到中间过程」，有逐条数据支撑。

## 一、最重要的知识

**Context Quarantine 隔离的是消息历史，不是文件。** 子 Agent 在独立上下文里执行，
中间的工具调用不进父上下文，父 Agent 只拿到子 Agent 的最后一条消息。
两者共用同一个 backend，所以文件是共享的——隔离的是「对话」不是「磁盘」。

**子 Agent 定义只有 3 个必填字段**：`name` / `description` / `system_prompt`。

**继承规则有个反直觉的地方**：

| 字段 | 继承主 Agent？ |
|---|---|
| `system_prompt` | 不继承，必须自己写 |
| `middleware` | 不继承 |
| `skills` | 不继承（只有 general-purpose 例外） |
| `tools` | 不写=继承全部；**写了=完全替换，不合并** |

**`tools` 的「完全替换」有个边界**：它替换的是**传进 `create_deep_agent(tools=)` 的那批工具**，
中间件提供的工具不受影响。实测 archivist 显式只给 2 个自定义工具，最终绑定 9 个——
7 个是 `FilesystemMiddleware` 注入的内置文件工具（`ls`/`read_file`/`glob`/…），一个没少。
想让子 Agent 连文件工具都没有，得动中间件，不是动 `tools=`。

**`task` 工具是单向的**：主 Agent 由 `SubAgentMiddleware` 自动注入 `task`，
子 Agent 拿不到 `task`——否则可以无限套娃。这一点实测确认，不是照着文档抄的。

**`description` 是唯一的路由依据。** 主 Agent 看不到子 Agent 的 system_prompt，
只靠 description 决定派给谁，所以描述要写成「什么时候用它」而不是「它是什么」。

**general-purpose 是唯一的例外**：它继承主 Agent 的 system_prompt / tools / model / skills。
v0.7 还多一条边界——主 Agent 显式传入的 `TodoListMiddleware` 会被 general-purpose 继承，
但 `subagents=[...]` 声明的专业子 Agent **不继承**，要各自在 `middleware` 里开。
继承的是「规划能力」，不是主 Agent 已经生成的那份清单；每个 Agent 维护自己的 todos。

**想彻底关掉子 Agent 机制，别去动中间件。** 用 `excluded_middleware` 排除 `SubAgentMiddleware`
会直接抛 `ValueError`。正确做法是注册一个 Harness Profile：

```python
from deepagents.profiles import (
    GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile,
)

register_harness_profile(
    key="openai:zai-org/GLM-5.2",
    profile=HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)
    ),
)
```

两步缺一不可：注册 profile，并且 `subagents=[]` 不传任何同步子 Agent。

**子 Agent 不用 `recursion_limit` 约束。** 它由 `create_agent` 构建时自带绑定 9999，
外层 `.with_config()` 覆盖无效——`subagents.py` 的 `ensure_config` 注释写明
「the subagent's bound config still wins collisions」。
要管子 Agent 只能在它自己的 `middleware` 里放 `ToolCallLimitMiddleware`——
上面两个子 Agent 各自带了 `run_limit=8` 的硬停，就是这个原因。

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

父上下文形状，一共 4 条消息：

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

archivist 的工具集里没有 `tavily_search`、没有 `task`；scout 的工具集里没有本地笔记工具。
两边的自定义工具互斥，内置文件工具都在——正是上面说的那条边界。

### 真实模型实跑

```
耗时 34.6s
  → task(subagent_type='note-archivist')
  → task(subagent_type='note-archivist')
父上下文消息数：5
```

两次委派都路由到 `note-archivist`（问题是「我的本地笔记里关于 write_todos 记了什么」，
本地笔记场景），返回内容带文件路径与行号如
`research_deepagent/frontend/public/work/deep-agents-write-todos.md L9`。
父上下文只有 5 条消息，子 Agent 的检索过程一条没漏进来。

复现：

```bash
cd research_deepagent
.venv/Scripts/python.exe ../_ch05_isolation_probe.py   # 机制，不联网不耗 token
.venv/Scripts/python.exe ../_ch05_live_run.py          # 真实模型
```

## 三、遇到的问题

### 1. 隔离检查「假通过」——角色键不匹配导致子 Agent 空跑

第一版探测返回 `[archivist script exhausted]`，但**隔离检查全部通过**。

根因：`_role_of()` 按工具名返回短名 `"archivist"`，而剧本字典的键是 `"note-archivist"`。
`scripts.get("archivist")` 拿到 `None` → 子 Agent 拿到空剧本 → 立刻返回哨兵值收尾。
子 Agent 压根没执行，中间过程当然不会污染父上下文——**检查通过是因为什么都没发生**。

定位靠的是把「每次 `_generate` 的 role / cursor / 剧本长度」打出来：

```
[dbg] _generate role=archivist cursor=0 script_len=0 msgs=2   ← script_len=0 露馅
```

修法两条：

1. 角色名与剧本键**逐字对齐**
2. 剧本为空时 `raise RuntimeError`，不要静默返回哨兵值——对不上就报错，
   比对上了却空跑安全得多

### 2. 脚本化假模型的可变状态不能放 pydantic 实例字段

写成实例字段后，`bind_tools` 返回的 `model_copy` 分身各自持有一份 `cursor`，
对 `int` 的自增留在了分身上，原实例永远读 0。ch07 探测里这个坑直接表现为
`GraphRecursionError`（模型无限重复 `read_file`）。

修法：所有「要跨调用累加 / 要事后读取」的状态放**模块级变量**，
绕开 `model_copy` 的浅拷贝语义。ch05、ch07 两个探测脚本现在都是这么写的。

## 四、收获

**假通过比失败更危险。** 失败会报错、会被看见；假通过会告诉你「10/10 通过」，
让你把一条根本没验证的结论写进笔记。要防它，就得让「前提不成立」这件事本身炸出来——
空剧本直接抛异常，是一行代码的代价。

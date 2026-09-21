# Task3 · 虚拟文件系统与两种后端的效果对比

> 2026-09-21 · Windows / Python 3.13.14 / deepagents 0.7.13 / GLM-5.2


## 一、知识

### 虚拟文件系统管的是上下文，不是存文件

工具结果超过 20000 tokens（`tool_token_limit_before_evict`）时自动卸载：完整内容写进虚拟 FS，对话里只留「文件路径 + 前 10 行预览」，需要时再 `read_file` 读回。摘要阈值默认取模型窗口的 85%，摘要前会把旧消息存进 Backend。

七个工具：`ls` / `read_file` / `write_file` / `edit_file` / `delete` / `glob` / `grep`。两个容易记错的点：

- `write_file` 是**整文件覆盖**，只改局部要用 `edit_file`
- `grep` / `glob` 会返回**有效但不完整**的结果（`truncated=True`），空结果的表现是 `No files found`——所以「没搜到」不等于「不存在」（这一点在 Task2 的坑 4 里已经吃过一次）

### 后端才是真正决定文件存哪的东西

`create_deep_agent(backend=...)`，不传就是 `StateBackend`（`deepagents/graph.py:637`）。

| 后端 | 文件实际存哪 | 关键性质 |
|---|---|---|
| `StateBackend`（默认） | LangGraph 的 `files` 状态通道 | 线程内持久、换 thread 就丢、主图与子代理共享 |
| `FilesystemBackend` | 真实磁盘 | 读写不可逆；`virtual_mode=True` 只拦 `..` / `~` / 越界绝对路径 |
| `CompositeBackend` | 按路径前缀路由到不同后端 | `default` + `routes={"/notes/": ...}` |

还有一条源码事实，决定了后面能不能把结论外推到子代理：

```python
# deepagents/graph.py:693（构建子代理中间件时）
FilesystemMiddleware(
    backend=backend,          # ← 就是主图那一个实例，没有另建
    ...
)
```

**子代理和主图共用同一个 backend 对象**，所以「主图看不到磁盘」等于「子代理也看不到」。

### StateBackend 不能在图外直接调用

想绕过 agent 直接 `StateBackend().glob(...)` 验证是不行的：

```
RuntimeError: StateBackend must be used inside a LangGraph graph execution
(e.g. via create_deep_agent). It cannot read or write state outside of a graph context.
```

它靠 LangGraph 的 `CONFIG_KEY_READ` / `CONFIG_KEY_SEND` 读写 state，图外拿不到 config。所以下面的实验必须把 backend 塞进 agent 里跑。

## 二、实验：同一组调用，三个后端

**方法**：写一个脚本化假模型接管 LLM，让它按固定顺序发出同一组 8 次文件工具调用，只替换 `backend`。变量只剩 backend 一个——不联网、不消耗 token、不依赖模型发挥，但走的仍是真实图执行链路（就是上一节那个 raise 要求的）。

脚本：`_ch03_backend_probe.py`（根目录，`.gitignore` 里的 `/_*.py` 已排除）。

三个被测对象：

```python
StateBackend()                                          # A 默认
FilesystemBackend(root_dir=仓库根, virtual_mode=True)     # B 真磁盘
CompositeBackend(                                       # C 混合路由
    default=StateBackend(),
    routes={"/notes/": FilesystemBackend(root_dir=Learn-Notes, virtual_mode=True)},
)
```

### 结果矩阵

| 检查项 | A `StateBackend` | B `FilesystemBackend`(仓库根) | C `CompositeBackend` |
|---|---|---|---|
| `ls /` | `No files found` | 整个仓库：`['/.cache/', '/.git/', '/Learn-Notes/', '/_ch02_demo.py', …]` | `['/notes/']` |
| `glob **/*.md` | `No files found` | 命中 306 个（笔记只占 2 个） | 2 个，全在 `/notes/` 下 |
| `grep 子代理` | `No matches found` | 命中 | 命中 2 个文件 |
| 写后读回 `/probe/hello.txt` | ✓ | ✓ | ✓ |
| 读磁盘笔记（原相对路径） | ✗ | ✓ | ✗ |
| 读 `research_deepagent/.env` | ✗ | **✓，含真钥** | ✗ |
| 写入落盘到磁盘 | ✗ 只在 state | ✓ 真实文件 | ✗ 落回 state |

### 关键返回原文

A 是「自洽的孤岛」——写进去读得回来，但磁盘上的东西一律不存在：

```
glob   **/*.md  → No files found
grep   子代理    → No matches found
read   Learn-Notes/Task1-AgentSeek环境搭建与踩坑.md
                → Error: File '/Learn-Notes/Task1-AgentSeek环境搭建与踩坑.md' not found
read   research_deepagent/.env
                → Error: File '/research_deepagent/.env' not found
```

B 能读到真东西，但把整个仓库都暴露了：

```
read   Learn-Notes/Task1-AgentSeek环境搭建与踩坑.md
                → 1 # Task1 · AgentSeek 环境搭建与踩坑
                  2
                  3 > 2026-09-14 · Windows / Python 3.13.14 / GLM-5.2
                  [Read 3 lines (lines 1-3 of 425 total). 422 lines remaining]
read   research_deepagent/.env
                → 1 # --- Model provider ---  2 # 智谱 GLM 走 OpenAI 兼容接口…
                  （键名全在，值已隐去；agent 拿到的是未隐去的原文）
```

`write_file /probe/hello.txt` 之后，磁盘上真的出现了文件：

```
probe/hello.txt  16 bytes  内容='written by agent'
```

C 是「只开一扇门」——`ls /` 里只有一个挂载点，`.env` 读不到：

```
ls     /        → ['/notes/']
glob   **/*.md  → ['/notes/Task1-AgentSeek环境搭建与踩坑.md',
                   '/notes/Task2-ch01-ch02-从Harness认知到自定义工具.md']
read   /notes/Task1-AgentSeek环境搭建与踩坑.md → ✓ 读得到
read   research_deepagent/.env                → Error: not found
read   Learn-Notes/Task1-AgentSeek环境搭建与踩坑.md → Error: not found
```

最后一行是 C 的代价：**挂载前缀换掉了路径语义**。笔记被挂在 `/notes/` 下，原来那套 `Learn-Notes/xxx.md` 的相对路径就失效了，提示词里得跟着改。

复现：

```bash
cd D:\Works\DeepAgents学习
research_deepagent/.venv/Scripts/python.exe _ch03_backend_probe.py
```

## 三、问题

**1. 假模型的 `bind_tools` 是抽象方法**

`BaseChatModel.bind_tools` 在 langchain 1.4 里没给默认实现，子类不写就抛 `NotImplementedError`——而且**消息是空的**，堆栈要翻到 `factory.py:1434` 才看得出是模型绑定而不是 backend 的问题。补一个返回 `self` 的实现即可（剧本自己决定发什么调用，不需要真绑工具）。

**2. `root_dir` 指向仓库根，等于把依赖目录一起交给 Agent**

`glob **/*.md` 上报 306 个命中，真正是笔记的只有 2 个：

| 位置 | `.md` 数量 |
|---|---|
| `research_deepagent/frontend`（node_modules） | 287 |
| `.cache/agentseek-skills` | 114 |
| `research_deepagent/.venv` | 48 |
| `Learn-Notes` | **2** |

`grep` 还因此超时，返回的是部分结果：

```
Grep of '/.' timed out after 15s with 5 matching file(s); returning partial results
```

（另一次运行里 `glob` 也报过 `timed out after 5s with 306 match(es)`。）所以 `root_dir` 必须收到具体目录，不能图省事给仓库根。

**3. `FilesystemBackend` 会把 `.env` 交给模型**

这不是理论风险，是上面 B 那一行实测：`read_file("research_deepagent/.env")` 直接返回文件内容。课程原文的警告在本地项目里同样成立——**这个 backend 不能接在有真钥的仓库根上**。C 用挂载点把它挡住了，是三者里唯一既能看到笔记、又读不到密钥的。

## 四、收获与待办

- **ch02 留下的缺陷根因坐实了**：编排层用 `glob` 核实子代理的本地引用、结果全是 `No files found`、进而判定「编造」——就是默认 `StateBackend` 的可见范围问题。子代理用的是同一个 backend 实例（`graph.py:693`），所以换掉 backend 等于同时修好两端。
- **修法确定选 C**，比 Task2 笔记里列的方案 A（把只读工具下放给主图）更彻底：A 只补上了我自建的 `read_local_note`，内置的 `ls` / `glob` / `grep` 依旧是瞎的；C 把内置工具一起点亮，而且只点亮 `/notes/`。
- 代价两条，改的时候要一起处理：提示词里引用笔记的路径要改成 `/notes/...`；`search_local_notes` / `read_local_note` 与内置 `glob` / `read_file` 功能开始重叠，得决定留谁。

待办：

- [ ] 把 `agent.py` 的 `create_deep_agent(...)` 加上 `backend=CompositeBackend(...)`，并把 `prompts.py` 里的笔记路径改成 `/notes/` 前缀
- [ ] 重新跑一次端到端，确认编排层的 `glob` 这次能核实子代理的引用
- [ ] 决定自建的两个本地检索工具去留（保留则明确与内置工具的分工）

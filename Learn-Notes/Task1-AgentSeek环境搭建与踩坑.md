# Task1 · AgentSeek 环境搭建与踩坑

> 2026-09-14 · Windows / Python 3.13.14 / GLM-5.2

---

用 AgentSeek CLI 生成一个深度研究 Agent，跑通了「前端 → AgentSeek API → LangGraph → deepagents → GLM-5.2 + Tavily」整条链路。题目是课程给的 `Research what LangGraph 1.0 added compared with 0.x. Cite sources.`，7 分 02 秒完成（子代理占 6 分 11 秒），产出 12,988 字符、10 条引用的报告，父图侧只有 3 次工具调用。结论是 LangGraph 1.0 属于稳定性里程碑，没有堆功能，唯一的破坏性变更是去掉 Python 3.9。

## 一、架构

### 1.1 三层委托关系

AgentSeek、deepagents、LangGraph 到底谁管谁，我一开始是分不清的。看完源码，实际是三层清晰的委托：

```mermaid
flowchart TB
    subgraph L1["① 脚手架层 · AgentSeek CLI"]
        A1["agentseek create<br/>生成项目骨架"]
        A2["agentseek task<br/>生命周期命令"]
        A3["agentseek doctor/dev<br/>就绪检查 + 起服务"]
    end

    subgraph L2["② 服务层 · AgentSeek API (0.2.3)"]
        B1["LangGraph Platform 兼容 HTTP API"]
        B2["/threads /runs /assistants"]
        B3["Checkpoint 持久化后端"]
    end

    subgraph L3["③ Agent 层 · deepagents (0.7.13)"]
        C1["create_deep_agent()"]
        C2["中间件栈"]
        C3["orchestrator + subagent"]
    end

    L1 -->|生成并管理| L2
    L2 -->|加载 langgraph.json 里的 graph| L3
    C1 --> D["模型: GLM-5.2<br/>工具: Tavily / think_tool"]
```

| 层 | 角色 | 关键点 |
|---|---|---|
| **AgentSeek** | 脚手架 + 运行时 | 它不是 Agent 框架，只是把 deepagents 项目包装成可运行的 LangGraph 服务（后端）+ React 前端 |
| **deepagents** | Agent 编排 | 提供 `create_deep_agent`，本质是「预设好一堆中间件的 `create_agent`」 |
| **LangGraph** | 执行引擎 | 图执行、checkpoint、流式、HITL |

三层各管一段：AgentSeek 管怎么跑起来，deepagents 管 Agent 怎么想，LangGraph 管怎么执行。

### 1.2 research graph 的真实结构

`langgraph.json` 里只注册了一个 graph：

```json
{ "graphs": { "research": "./src/research_deepagent/agent.py:graph" } }
```

编译后的父图只有 3 个真实节点（内省确认）：

```
PatchToolCallsMiddleware.before_agent  →  model  →  tools
```

这里有个容易踩的认知差：**子代理不是图中的节点**。它们是 `SubAgentMiddleware` 注册的 `task` 工具，通过工具调用内联执行。理解了这一点，第 3 节坑 7 里「`stream_mode=["updates"]` 看不到子代理搜索过程」就不奇怪了。

### 1.3 模型实际拿到的工具集

我拦截了 `create_agent` 的调用点，抓到模型绑定的完整工具集，一共 11 个：

| 来源 | 工具 |
|---|---|
| 我传的 `tools=[...]` | `tavily_search`、`think_tool` |
| FilesystemMiddleware | `ls`、`read_file`、`write_file`、`edit_file`、`delete`、`glob`、`grep`、`execute` |
| SubAgentMiddleware | `task` |

对应的中间件栈（按装配顺序）：

```
FilesystemMiddleware  →  SubAgentMiddleware  →  _DeepAgentsSummarizationMiddleware
                      →  PatchToolCallsMiddleware  →  AnthropicPromptCachingMiddleware
```

我没写一行文件操作代码，模型却拿到了 8 个文件系统工具。这就是 deepagents 的卖点——开箱即用的 harness。

## 二、环境搭建

### 2.1 版本矩阵

| 组件 | 版本 |
|---|---|
| agentseek (CLI) | 0.1.4 |
| agentseek-api | 0.2.3 |
| deepagents | 0.7.13 |
| langchain | 1.4.0 |
| langgraph | 1.2.11 |
| Python / uv | 3.13.14 / 0.12.3 |
| Node / npm | 22.22.2 / 10.9.7 |
| vite | 8.3.0 |

`npm 10.9.7` 这个版本号后面还会出现两次——坑 1 和坑 2 都是它引起的。

### 2.2 命令序列

```bash
uv tool install --upgrade agentseek
uv tool update-shell                      # 把 ~/.local/bin 持久化进 PATH

agentseek create deepagents/research --checkout main --no-input
cd research_deepagent

agentseek task sync                        # 后端依赖（uv sync）
agentseek task frontend                    # 前端依赖 —— 会失败，见坑 2
cd frontend && npm install                 # 手动替代

# 填 .env（见 2.3）
agentseek doctor                           # 全 ok 才继续
agentseek dev                              # 后端 2024 / 前端 5174
```

### 2.3 `.env` 关键配置

```env
# --- 模型：智谱 GLM-5.2 走 OpenAI 兼容接口 ---
AGENTSEEK_MODEL_PROVIDER=openai            # 国产模型也填 openai，别造 GLM_API_KEY
AGENTSEEK_MODEL=glm-5.2
OPENAI_API_KEY=<你的智谱 Key>
OPENAI_API_BASE=https://open.bigmodel.cn/api/paas/v4
LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S=300

# --- 持久化：Windows 必须切 SQLite（见坑 3）---
SEEKDB_EMBED=false
METADATA_DB_BACKEND=sqlite
METADATA_DB_URL=sqlite+aiosqlite:///C:/Users/123/.agentseek/research_deepagent/agentseek.db

# --- 搜索 ---
TAVILY_API_KEY=tvly-dev-...
```

`LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S=300` 是必须改的。LangChain 的 OpenAI 客户端有个 chunk 间隔超时，默认 120s；智谱网关在流式返回大 tool-call 负载时会停顿较久，超过 120s 就断流，放到 300s 才稳。

`.env` 已被模板 `.gitignore` 覆盖，Key 不会进 git。

## 三、踩的坑

下面 5 个坑都在本机真实复现并修复过，每个都做了对照实验排除误判。

### 坑 1：npm 10 在 vitest 的 peer 依赖环上崩溃

`agentseek task frontend` 装依赖时崩了：

```
TypeError: Cannot read properties of null (reading 'edgesOut')
    at #loadPeerSet (arborist/build-ideal-tree.js:1289)
```

先怀疑 npmmirror 镜像损坏，拿镜像和官方源的 manifest 对了一遍，两边都正常；又怀疑是中文路径编码，换到纯 ASCII 路径 `C:\Users\123\npm-prefix-test` 做对照，照样复现；最后加 `--legacy-peer-deps` 重试，216 个包一次装成功，问题才锁定在 peer 解析上。

根因是 npm 10.x 的 arborist 解析 vitest 4.x 的 peer 依赖**环**时崩溃——`@vitest/browser-playwright@5.0.0` 反向 peer 回 vitest 5.x，形成循环。这是 npm 的已知 bug，11 才修。

修改不动模板原文件，新建 `frontend/.npmrc`：

```
legacy-peer-deps=true
```

前端依赖（react、@langchain/core）都在 `package.json` 里显式声明了，跳过 peer 解析不影响运行。

### 坑 2：模板的 `npm install --prefix frontend` 在 npm 10 下静默失效

`agentseek task frontend` 报：

```
ENOENT: no such file or directory, open '...\research_deepagent\package.json'
```

我手动把四种写法都试了一遍，全失败：

| 命令 | 结果 |
|---|---|
| `npm install --prefix frontend` | ❌ 找根目录 package.json |
| `npm --prefix frontend install` | ❌ 同上 |
| `npm install --prefix ./frontend` | ❌ 同上 |
| 纯 ASCII 路径下任意写法 | ❌ 同上 |

根因是 npm 10 对本地目录忽略 `--prefix`（这个参数实际只对全局安装生效），会退回从 cwd 向上找 `package.json`。模板里这条 task 声明在 npm 10 下就是坏的。

AgentSeek 的 `TaskV2` 支持 `cwd` 字段（源码在 `agentseek/cli/lifecycle/authored.py`），所以改 `.agentseek/lifecycle.toml`：

```toml
[tasks.frontend]
description = "Install frontend dependencies."
command = ["npm", "install"]
cwd = "frontend"          # ← 用 cwd 而不是 --prefix
```

### 坑 3：嵌入式 SeekDB 在原生 Windows 上必然启动失败

```
RuntimeError: Embedded Client is not available because pylibseekdb is not available.
Please install pylibseekdb (Linux only) or use RemoteServerClient (host/port) instead.
```

这个坑的迷惑性在于表象：在外面看到的是 `agentseek dev` 卡在 `Timed out waiting for 'http://localhost:2024' to become ready`，前端却正常起来了，很容易以为是后端慢。

模板默认 `SEEKDB_EMBED=true`，用嵌入式 SeekDB 做 checkpoint 持久化，而 `pylibseekdb` 官方只有 Linux 版。课程文档给的方案是「Windows 用 WSL2」，但本机 WSL 被安全策略禁用，这条路走不通。

我去读 `agentseek_api/core/database.py`，发现 `else` 分支支持纯本地方案，可以切到 agentseek-api 自带的 SQLite 后端（`SqliteCheckpointSaver` + `InMemorySaver` + `SqliteStore`）：

```env
SEEKDB_EMBED=false
METADATA_DB_BACKEND=sqlite
METADATA_DB_URL=sqlite+aiosqlite:///C:/Users/123/.agentseek/research_deepagent/agentseek.db
```

两个细节：`METADATA_DB_URL` 的父目录要先手工 mkdir（非 embed 模式不会自动创建）；路径用正斜杠 + 三斜杠 + 盘符。

验证通过：`/health` 返回 `{"status":"healthy"}`，checkpoint 库 28MB 正常增长。

这次最大的收获是——遇到平台不兼容时，报错信息给的方案（「去装 WSL」）只是它能想到的最省事的方案，不一定是我环境里可行的方案。降级分支要自己去依赖库源码里翻。

### 坑 4：`TAVILY_API_KEY` 是硬门槛，会拦住整个服务启动

`deepagents/research` 模板把 Tavily 标为必需项，`agentseek dev` 会先跑 doctor，缺 key 直接拒绝启动。

想先确认「其它部分是否健康」时，可以用占位值：

```bash
TAVILY_API_KEY=tvly-placeholder agentseek dev
```

正式做法是去 `https://app.tavily.com` 注册免费额度（1000 次/月，跑课程足够），填进 `.env`。

### 坑 5：内联环境变量会静默覆盖 `.env`

做坑 4 的占位测试时用了 `TAVILY_API_KEY=tvly-placeholder agentseek dev`。之后换成真实 Key 重启，却一直用的还是占位值，而且不报任何错。

原因是 `python-dotenv` 的 `load_dotenv()` 默认不覆盖已存在的环境变量（`override=False`），shell 里传的内联变量优先级更高。

重启时显式清掉就行：

```bash
unset TAVILY_API_KEY && agentseek dev
```

做「不改 .env 的临时启动测试」时要记住，注入的变量会在后续会话里一直生效。

## 四、两个机制层面的发现

### 发现 6：deepagents 的 `write_file` 不写真实磁盘

跑完研究任务后，我去项目目录找 `final_report.md`，没找到。第一反应是任务失败了，其实不是。

`deepagents/graph.py:637`：

```python
backend = backend if backend is not None else StateBackend()
```

默认 backend 是 `StateBackend`，文件落在 graph state 的虚拟文件系统里，不是硬盘。

正确的取法是从线程 state 里读：

```python
# GET /threads/{tid}/state
vals = state["values"]
files = vals["files"]        # {'/research_request.md': {...}, '/final_report.md': {...}}
report = files["/final_report.md"]["content"]     # ← 正文在这里
```

已经封装成 `_extract_report.py`。

要真正落盘，得在 `create_deep_agent` 里显式传 `FilesystemBackend` 或 `CompositeBackend`。deepagents 提供了 8 种 backend：`state` / `filesystem` / `store` / `composite` / `sandbox` / `local_shell` / `langsmith` / `context_hub`。这是把 Agent 接进真实工程环境的关键扩展点。

### 发现 7：模板的 system prompt 要求调用一个不存在的工具

`prompts.py` 里 `RESEARCH_WORKFLOW_INSTRUCTIONS` 第一条写得很硬：

```
1. **Plan first**: Before using task() or write_file(), you MUST call write_todos
   to create a todo list that breaks the research into focused tasks
```

但我内省出的 11 个工具里没有 `write_todos`：

```
delete, edit_file, execute, glob, grep, ls, read_file, task, tavily_search, think_tool, write_file
```

`write_todos` 来自 `langchain.agents.middleware.todo.TodoListMiddleware`，而 deepagents 0.7.13 的基础中间件栈（`graph.py:862-891`）只装配了 Filesystem / SubAgent / Summarization / PatchToolCalls，没有 TodoList。

端到端跑起来，编排层确实直接跳到 `write_file` + `task`，完全没有规划步骤：

```
15.9s  model  → tool:write_file        # 写研究大纲
15.9s  model  → tool:task              # 直接委派，没有 todos
```

这是课程模板和 deepagents 版本演进之间的错配——prompt 写的是老版本的行为。影响不算致命，但对多步研究类任务，缺少显式规划会让任务分解质量打折。

要修的话，在 `agent.py` 的 `create_deep_agent(...)` 里补 `middleware=[TodoListMiddleware()]`（从 `langchain.agents.middleware` 导入），或者把 prompt 里 `write_todos` 那段删掉。留一段永远执行不到的指令没有意义。

## 五、GLM-5.2 实测

### 5.1 能力探测

配好之后先跑探测，别等整个服务起来再 debug：

| 链路 | 结果 |
|---|---|
| 基础对话 | 通过 |
| 流式 | 通过，TTFT 0.72s |
| 工具调用 | 通过，LangChain `bind_tools` 能并行发多个 tool call |

### 5.2 思考模式延迟

GLM-5.2 默认开启思考，会先生成 reasoning tokens：

| 模式 | 简单问题延迟 | reasoning tokens |
|---|---|---|
| 默认（思考开） | 2.6 – 3.4s | ~100 – 170 / 问 |
| `thinking: disabled` | 0.7 – 0.8s | 0（output_tokens 降到 1） |

关闭方式：

```python
model.bind(extra_body={"thinking": {"type": "disabled"}})
```

这个 Agent 我保留了思考模式。关掉虽然快 4 倍，但 output_tokens=1 这种结果说明质量确实掉了；单次任务要跑几十轮调用，延迟主要花在子代理搜索上，省这点生成时间不划算。

探测时遇到过一次 47s，当时以为思考模式不可用，连测 3 次才确认那只是长回答的个例。

### 5.3 端到端时间线

```mermaid
flowchart LR
    A["0s<br/>任务提交"] --> B["15.9s<br/>编排层 write_file 写大纲<br/>task 委派子代理"]
    B --> C["16.9s → 388.3s<br/>子代理执行 371s<br/>父图流里无任何输出<br/>返回 16179 字符素材"]
    C --> D["388.8s<br/>写终稿 /final_report.md"]
    D --> E["418.6s<br/>输出 1716 字符摘要<br/>总耗时 421.7s"]
```

`stream_mode=["updates"]` 下，17s → 388s 这段时间什么输出都没有。这不是卡死，是子代理在内联执行，父图的 updates 流看不到。给超时的时候要给足，建议 ≥1800s。

## 六、端到端验证脚本

只看 `/health` 返回 200 不够，要真跑一次任务。AgentSeek API 是 LangGraph Platform 兼容接口（`auth_type=noop`，无需鉴权）：

```python
import json, urllib.request
BASE = "http://127.0.0.1:2024"
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # ← 关键

def post(p, body):
    req = urllib.request.Request(BASE+p, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return op.open(req, timeout=1800)

# 1. 取 assistant_id（graph_id = "research"）
a = json.loads(post("/assistants/search", {}).read())[0]
# 2. 建线程
tid = json.loads(post("/threads", {}).read())["thread_id"]
# 3. 流式跑
post(f"/threads/{tid}/runs/stream", {
    "assistant_id": a["assistant_id"],
    "input": {"messages": [{"role": "user", "content": "Research ... Cite sources."}]},
    "stream_mode": ["updates"],
})
# 4. 取报告（注意是虚拟文件系统）
vals = json.loads(op.open(f"{BASE}/threads/{tid}/state").read())["values"]
report = vals["files"]["/final_report.md"]["content"]
```

三个坑点：

| 要点 | 说明 |
|---|---|
| `GET /assistants` → 405 | 必须用 `POST /assistants/search`，body 传 `{}` |
| 必须绕代理 | 沙箱 Bash 里直接 curl localhost 会被网络代理拦成 502 upstream connect failed，用 `ProxyHandler({})` |
| 超时要给足 | 默认 120s 必挂，用 1800s |

## 七、pre02：给编码助手装技能

```bash
npx skills add ob-labs/agentseek --skill langchain-dev-guide --skill langsmith-trace --agent '*' --yes
npx skills list
```

项目级落到 `.agents/skills/`（Codex / Cursor / Gemini CLI 通用目录），并 symlink 到 `.claude/skills/`。`--agent '*'` 会自动跳过没装的助手，报 `skipped ... (project directory not found)` 是正常的。

GitHub clone 中途断过一次（`Failure when receiving data from the peer`），解法是先本地浅克隆，再让 CLI 从本地路径装：

```bash
git clone --depth 1 https://github.com/ob-labs/agentseek.git <cache-dir>
npx skills add "<cache-dir-abs-path>" --skill langchain-dev-guide --skill langsmith-trace --agent '*' --yes
```

## 八、速查表

| # | 坑 | 现象关键词 | 根因 | 修法 |
|---|---|---|---|---|
| 1 | npm arborist 崩溃 | `Cannot read properties of null (reading 'edgesOut')` | npm 10.x peer 环解析 bug（vitest 4.x） | `frontend/.npmrc` 加 `legacy-peer-deps=true` |
| 2 | `--prefix` 失效 | `ENOENT ...\research_deepagent\package.json` | npm 10 忽略本地 `--prefix` | `lifecycle.toml` 改用 `cwd = "frontend"` |
| 3 | SeekDB 不兼容 | `pylibseekdb is not available (Linux only)` | 嵌入式 SeekDB 只有 Linux 版 | 切 `SEEKDB_EMBED=false` + `sqlite` |
| 4 | Tavily 硬门槛 | doctor 拦下 `agentseek dev` | 模板标为 required | 注册免费 Key；临时可用占位值 |
| 5 | 环境变量覆盖 | 换 Key 后仍用旧值，无报错 | dotenv `override=False` | `unset VAR && agentseek dev` |
| 6 | 报告找不到 | 项目目录无 `final_report.md` | `StateBackend` 是虚拟文件系统 | 从 `/threads/{tid}/state` 取 |
| 7 | `write_todos` 不存在 | prompt 强制要求但工具集里没有 | deepagents 0.7.13 未装配 `TodoListMiddleware` | 补中间件，或删 prompt 里的死指令 |

## 九、收获与待办

这次最有用的两条经验都不是关于 Agent 本身的。

一条是遇到平台不兼容时，别只盯着报错给的方案。SeekDB 那个坑，报错让我去装 WSL，但本机 WSL 被安全策略禁了，真正的解法是我翻 `database.py` 的 `else` 分支翻出来的。报错信息只给「最省事的方案」，不给「你环境里能用的方案」。

另一条是排查要有对照实验。npm 那个坑，我先后排除了「镜像损坏」和「中文路径」两个错误假设，才定位到版本 bug。凭第一直觉改配置，大概率是在错误的地方打补丁。

另外两点和 deepagents 有关。harness 这套「中间件栈 + 可插拔 backend」的抽象，意味着同一份 Agent 逻辑能跑在内存 / 本地磁盘 / 沙箱 / 远程存储上——这一点对把 Agent 从 demo 推到产线挺关键。还有 prompt 和工具集必须对齐，发现 7 就是反例：一段看起来很规范的强制指令，实际永远执行不到。

### 待办

- [ ] **把报告真正落盘**：当前 `write_file` 只写虚拟 FS。考虑改用 `CompositeBackend(filesystem=FilesystemBackend(root_dir=...))`，让 `/final_report.md` 直接落到本地磁盘
- [ ] **补 `TodoListMiddleware`**：修复发现 7 的 prompt / 实现错配，观察规划步骤对研究质量的影响
- [ ] **压测并发**：当前 `MAX_CONCURRENT_RESEARCH_UNITS=3`，试试提到 5 对总耗时的影响
- [ ] **接 LangSmith**：`.env` 里 `LANGSMITH_TRACING` 还是 false。跑一次 trace 看子代理的调用链，验证「父图 updates 看不到子代理」这个结论
- [ ] **探索 backend 扩展**：deepagents 提供 8 种 backend，`store` / `context_hub` 这两种和「跨会话记忆」直接相关，值得单独研究


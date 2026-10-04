# Task7 · 长期记忆与 Human-in-the-Loop

环境：Windows + `research_deepagent`，deepagents 0.7.13 / langchain 1.4.0 / langgraph 1.2.11。
代码在 `research_deepagent/src/research_deepagent/ch08/`（`memory_agent.py`）与
`.../ch09/`（`hitl_agent.py`），实跑脚本 `_ch08_memory_live.py`、`_ch09_hitl_live.py`。

ch08 用两个**不同的 thread_id** 验证了记忆真的跨对话：对话 1 写入偏好、对话 2 在全新
线程里遵守它，3/3 通过；ch09 把 approve / edit / reject / respond 四种决策各走了一遍
完整流程，5/5 通过。两处最值得记的：**系统提示词不在 `result["messages"]` 里**，
以及**模型倾向顺序调用工具，所以链式中断比批量中断更常见**。

## 一、最重要的知识

### 两种记忆的边界（ch08）

| | 短期记忆 | 长期记忆 |
|---|---|---|
| 载体 | `StateBackend`（graph state 里的虚拟 FS） | `StoreBackend`（langgraph Store） |
| 作用域 | 单个 `thread_id` 内，靠 Checkpointer 持久化 | 跨 thread，按 namespace 隔离 |
| 换 thread 后 | 没了 | 还在 |

两者用 `CompositeBackend` 按**路径前缀**组合：`/memories/` 走 Store，其余留在 state。

**`memory=` 是读取配置，不是创建。** 0.7.10 起缺失的记忆文件被**静默跳过**——
不报错，路径也不会作为"已加载记忆"进提示词。要固定写入位置，得在提示词里另行约定。

**Store key 不带挂载前缀。** Agent 可见路径是 `/memories/preferences.md`，
但 `store.put()` 的 key 是 `/preferences.md` —— `CompositeBackend` 路由时会剥掉前缀。
写错 key 的症状是"明明 put 了，Agent 却看不到"。

**验证跨对话必须换新 `thread_id`。** 同一线程继续对话会复用 state 里已有的
`memory_contents`，那不叫重新加载——这是官方文档专门点出来的坑。

**写入方式要区分**：`write_file` 完整覆盖，追加/局部修订用 `edit_file`。

### 中断与恢复的四条硬要求（ch09）

1. 必须配 Checkpointer（HITL 靠它保存中断现场）
2. 必须用**同一个 `thread_id`**
3. 批量中断时 `decisions` 的**数量和顺序**要与 `action_requests` 一一对应
4. `edit` 决策的参数键固定是 `args`（不是 `arguments`），且必须带工具名

**四种决策别用混**：

| 决策 | 含义 | 用在哪 |
|---|---|---|
| `approve` | 按原参数执行 | 确认无风险 |
| `edit` | 改参数后执行 | 收件人/路径写错 |
| `reject` | 跳过调用并把原因反馈给模型 | **拒绝副作用工具只能用它** |
| `respond` | 人代替工具返回"成功"结果 | 仅限 `ask_user` 这类询问型工具 |

⚠️ 拒绝删除/发送时**不要用 `respond`**：它的 message 会被模型当作一次成功的
ToolMessage 收下，模型以为文件真删了。

**底层机制**：`interrupt()` 靠**抛异常**暂停，恢复时**节点从头重放**。所以
`interrupt()` 之前的副作用必须幂等；也不能用裸 `try/except` 包它（会吞掉中断异常）。

## 二、实践结果

### ch08：两轮全新对话验证跨对话记忆（3/3）

```
预置偏好文件：已写入（key=/preferences.md）

对话 1 · thread=82c37906（新对话）
用户：记住我的偏好：代码注释用中文，变量名用英文。
  → read_file(/memories/preferences.md)
  → ls()
  → write_file(/memories/preferences.md)
助手：已记住你的偏好：代码注释用中文 / 变量名用英文

从 Store 侧核对（不看模型说了什么）：
  ✓ 偏好文件存在
    # 用户偏好
    ## 代码风格
    - 代码注释使用中文
    - 变量名使用英文

对话 2 · thread=371b72a7（另一个新对话，与对话 1 不共享 state）
用户：写一个快速排序函数，把完整代码直接贴在回复里。
实际发给模型的系统提示词 11863 字符，其中：
  ✓ 含 /memories/preferences.md
  ✓ 含偏好正文
Agent 回答：注释全中文（928 字）
```

三条都通过：偏好写进了 Store、**新线程**的系统提示词里带着它、产出真的遵守了偏好。
第三条是"记忆生效"的可见证据——前两条只说明数据到位了。

### ch09：四种决策各走一遍（5/5）

| 场景 | 中断动作 | 决策 | 磁盘上的后果 |
|---|---|---|---|
| ① approve | `delete_note` | approve | `draft.md` 真被删 |
| ② edit | `send_email` | edit 改收件人 | outbox 里是 `01-team_at_example.com.txt` |
| ③ reject | `delete_note` | reject + 原因 | 文件还在 |
| ④ respond | `ask_user` | respond | 人的回答成为工具结果 |
| ⑤ 链式 | `delete_note` → `send_email` | approve → reject | 文件删了、outbox 未增加 |

每个场景都核对**磁盘上的真实后果**，不只看模型说了什么。场景 ③ 的拒绝原因
（"这是归档资料，不要重试删除"）也回到了 Agent 手里。

复现：

```bash
cd research_deepagent
.venv/Scripts/python.exe ../_ch08_memory_live.py   # 两轮对话，约 1 分钟
.venv/Scripts/python.exe ../_ch09_hitl_live.py     # 五个场景，约 3 分钟
```

## 三、遇到的问题

### 1. 断言写死字符串 → 把「生效了」判成「失败」

第一版断言 `"代码注释用中文" in stored`，模型写的是「代码注释**使用**中文」，
语义等价但字面不同，检查直接判负。**而功能其实完全正常。**
改成关键要素共现（`注释` + `中文` + `变量` + `英文`）才判得准。

教训：对**模型产出的自然语言**做断言，只能查要素，不能查原句。

### 2. 系统提示词不在 `result["messages"]` 里

第一版想从 `result["messages"]` 里找 `SystemMessage` 来验证 `memory=` 是否注入，
结果一个都找不到——那是**图状态**，只有 Human / AI / Tool 三类。

正确做法是拦模型调用，用 callback 抓真正发出去的 `SystemMessage`：

```python
class SystemPromptSpy(BaseCallbackHandler):
    def on_chat_model_start(self, serialized, messages, **kwargs):
        for batch in messages:
            for message in batch:
                if isinstance(message, SystemMessage):
                    self.systems.append(str(message.content))

agent.invoke(..., config={..., "callbacks": [spy]})
```

### 3. 本地中断在 `result["__interrupt__"]`，没有 `result.interrupts`

官方示例写 `result.interrupts[0].value`，本地 `CompiledStateGraph.invoke()` 上没有
这个属性。实测返回的是 **dict**，keys 为 `['__interrupt__', 'files', 'messages']`，
中断对象带 `.value` 与 `.id`。

所以文档说的 `version="v2"` 是 Platform/SDK 场景的接口形态；本地传统路径
读 `result["__interrupt__"]` 即可。

```
__interrupt__：[Interrupt(value={'action_requests': [
    {'name': 'delete_note', 'args': {'name': 'draft.md'}, 'description': ...}],
    'review_configs': [{'action_name': 'delete_note',
                        'allowed_decisions': ['approve', 'edit', 'reject']}]},
  id='b89f36d6...')]
```

注意负载里是 **`args`** 键（不是 `arguments`）。

### 4. 「批量中断」的预期落空——模型是顺序调用的

我原本设计了一个场景：让 Agent 一次请求里同时删文件 + 发邮件，期望看到
**一个中断打包两个动作**。实测模型**顺序**调用工具（先删，再发），形成的是
**链式中断**：批准第一个动作后，第二个敏感动作被独立再拦一次。

这不是缺陷——每个动作照样各自过闸，安全性不打折。把场景改成验证链式拦截后
断言才成立。**批量中断能不能出现，取决于模型是否真的并行发起 tool_call，
不能当成默认行为来设计。**

### 5. 提示词要求 `edit_file`，模型用了 `write_file`

ch08 对话 1 的轨迹是 `read_file → ls → write_file`，而系统提示词明确写的是
"Use edit_file to add the new entry"。因为 v0.7 的 `write_file` 是**完整覆盖**，
风险很实在：模型如果只写新条目、忘了把旧偏好带上，历史记忆就被清空了。
本次它把旧内容一起重写了所以没出问题，但这是运气。

要真正防住，得靠机制而不是提示词——比如给 `/memories/` 挂
`FilesystemPermission(operations=["write"], paths=["/memories/**"], mode="interrupt")`，
让人审一眼写回的内容，或者改用追加式日志 + 后台合并。

## 四、收获

**「写进去了」和「用上了」要分开验证。** ch08 的三条检查正好是三个层级：
Store 里有文件（数据到位）→ 系统提示词里带上了（路由到位）→ 产出遵守了（生效）。
只查第一条，一个"文件躺着没人读"的 bug 也能全绿。

**给模型产出写断言，要写"要素"不写"原句"。** 同一个意思它每次换一种说法，
字面断言必然假阴性——而假阴性比真失败更烦人，它让一个正常的功能看起来是坏的。

**框架的"便捷入口"不等于能力边界。** `interrupt_on` 覆盖了工具级审批，
但真正决定能不能拦住的，是 Checkpointer + 同一 `thread_id` + 决策与动作一一对应
这几条运行时约束。把这些当成配置项背下来，比背参数表有用。

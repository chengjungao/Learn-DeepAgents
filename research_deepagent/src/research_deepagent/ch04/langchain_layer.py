"""ch04 实战之二：把 TodoListMiddleware 装到 LangChain 更底层的 create_agent 上。

用途不是"再讲一遍原理"，而是回答一个具体问题：
**同样一套 Harness 能力，在 create_deep_agent 和 create_agent 之间，我到底要自己补多少东西？**

Deep Agents 帮我们自动装的是：文件系统、上下文压缩、工具调用修补、子代理。
下面手动把这几个中间件配齐，跑同一个假模型，对齐两次的工具清单。
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware import FilesystemMiddleware


class SpyModel(BaseChatModel):
    """只记录「本次绑了哪些工具」，不发任何调用。"""

    seen: list[list[str]] = Field(default_factory=list)
    replies: int = 0

    @property
    def _llm_type(self) -> str:
        return "spy"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        names = []
        for t in tools or []:
            if isinstance(t, dict):
                names.append(t.get("function", {}).get("name") or t.get("name", "?"))
            else:
                names.append(getattr(t, "name", str(t)))
        self.seen.append(sorted(names))
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager: CallbackManagerForLLMRun | None = None, **kwargs: object) -> ChatResult:  # noqa: ANN001
        self.replies += 1
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="ok"))]
        )


def tools_of(factory, cfg_label: str) -> tuple[list[str], list[str]]:
    """返回 (绑给模型的工具, 图节点)。

    两边的 SpyModel 都要**同一个实例**：bind_tools 记在模型上，
    图跑完再去读，中间换个实例就白记了。
    """
    model = SpyModel()
    g = factory(model)
    g.invoke(
        {"messages": [{"role": "user", "content": "hi"}]},
        config={"configurable": {"thread_id": cfg_label}, "recursion_limit": 40},
    )
    return (model.seen[0] if model.seen else []), list(g.get_graph().nodes)


def main() -> int:
    backend = StateBackend()

    # 1) Deep Agents：一行 Harness，Todo 显式加
    def deep(model):
        return create_deep_agent(
            model=model,
            system_prompt="x",
            middleware=[TodoListMiddleware()],
            backend=backend,
        )

    # 2) LangChain 底层：Todo + 文件系统手动加
    def low(model):
        return create_agent(
            model=model,
            tools=[],
            middleware=[
                TodoListMiddleware(),
                FilesystemMiddleware(backend=backend),
            ],
        )

    for label, factory in (("create_deep_agent(middleware=[Todo])", deep), ("create_agent(middleware=[Todo, Filesystem])", low)):
        names, nodes = tools_of(factory, label)
        print("=" * 78)
        print(label)
        print("-" * 78)
        print(f"  工具 {len(names)} 个：{names}")
        print(f"  图节点：{nodes}")
        print()

    print("=" * 78)
    print("结论")
    print("-" * 78)
    print("  create_deep_agent 少写的是 Filesystem / Summarization / PatchToolCalls")
    print("  等默认能力；Todo 两边都要自己加——v0.7 不默认提供。")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

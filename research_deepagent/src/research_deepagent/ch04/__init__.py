"""ch04 实战：任务规划能力。

对应课程 ch04（任务规划与分解）。两个可运行入口：

- `todo_doc_agent.py` —— 一个真正的小型文档 Agent，
  显式启用 `TodoListMiddleware`、最小工具集、产物落盘。
- `langchain_layer.py` —— 把同一套能力装到 LangChain 底层
  `create_agent()` 上，对比 `create_deep_agent()` 到底替我们省了什么。

两者的共同前提：**v0.7 不再默认安装 `TodoListMiddleware`**，
不显式传入 `middleware=[TodoListMiddleware()]` 就没有 `write_todos` 工具。
"""

from research_deepagent.ch04.todo_doc_agent import build_agent, run

__all__ = ["build_agent", "run"]

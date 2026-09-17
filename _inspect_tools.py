"""拦截 create_agent，列出中间件注册的工具 + 最终模型可见工具全集。"""
import warnings
warnings.filterwarnings("ignore")

import deepagents.graph as dg

_orig = dg.create_agent


def spy(*args, **kwargs):
    mw = kwargs.get("middleware", [])
    print("=== 中间件注册的工具 ===")
    allnames = set()
    for m in mw:
        ts = getattr(m, "tools", None) or []
        names = []
        for t in ts:
            n = t.name if hasattr(t, "name") else str(t)
            names.append(n)
            allnames.add(n)
        print(f"  {type(m).__name__:42} -> {sorted(names)}")
    for t in kwargs.get("tools", []) or []:
        allnames.add(t.name if hasattr(t, "name") else str(t))
    print()
    print("=== 模型可见工具全集 ===")
    for n in sorted(allnames):
        print("  -", n)
    print()
    print("write_todos 是否可见:", "write_todos" in allnames)
    return _orig(*args, **kwargs)


dg.create_agent = spy
import research_deepagent.agent  # noqa: E402,F401
print("done")

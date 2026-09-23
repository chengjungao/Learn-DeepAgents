"""Research tools for the deepagent.

Upstream tools
--------------
``tavily_search``
    Web discovery via Tavily, then fetch each hit and convert the page to
    markdown. Online, metered, API-key gated.
``think_tool``
    No-op reflection sink used to slow the agent down after each search.

These two mirror the upstream
``langchain-ai/deepagents/examples/deep_research/research_agent/tools.py``
apart from the default ``max_results`` / ``topic`` values, which are wired
to cookiecutter variables so the user can tune at scaffold time.

Locally added tools
-------------------
``search_local_notes`` / ``read_local_note``
    Keyword search and file reading over a local directory of notes. They
    exist because deepagents' default filesystem backend is ``StateBackend``
    — a virtual filesystem living inside graph state
    (``deepagents/graph.py:637``) — so the built-in ``read_file`` / ``grep``
    tools deliberately cannot reach real files on disk. Tavily covers the
    open web; these cover the user's own notes and project docs, for free
    and offline.

All four follow the three-part tool contract from the course (ch02):
typed parameters + an ``Args:`` docstring + defaults on optional arguments.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from langchain_core.tools import InjectedToolArg, tool
from markdownify import markdownify
from tavily import TavilyClient
from typing_extensions import Annotated, Literal

tavily_client = TavilyClient()


def fetch_webpage_content(url: str, timeout: float = 15.0) -> str:
    """Fetch and convert webpage content to markdown.

    Args:
        url: URL to fetch.
        timeout: Request timeout in seconds.

    Returns:
        Webpage content as markdown, or an error string on failure.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        )
    }
    try:
        response = httpx.get(
            url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        return markdownify(response.text)
    except Exception as exc:
        return f"Error fetching content from {url}: {exc!s}"


@tool(parse_docstring=True)
def tavily_search(
    query: str,
    max_results: Annotated[int, InjectedToolArg] = 3,
    topic: Annotated[
        Literal["general", "news", "finance"], InjectedToolArg
    ] = "general",
) -> str:
    """Search the web for information on a given query.

    Uses Tavily to discover relevant URLs, then fetches and returns full
    webpage content as markdown.

    Args:
        query: Search query to execute.
        max_results: Maximum number of results to return.
        topic: Topic filter — 'general', 'news', or 'finance'.

    Returns:
        Formatted search results with full webpage content.
    """
    search_results = tavily_client.search(
        query,
        max_results=max_results,
        topic=topic,
    )

    result_texts: list[str] = []
    for result in search_results.get("results", []):
        url = result["url"]
        title = result["title"]
        content = fetch_webpage_content(url)
        result_texts.append(
            f"## {title}\n"
            f"**URL:** {url}\n\n"
            f"{content}\n\n"
            "---\n"
        )

    return (
        f"🔍 Found {len(result_texts)} result(s) for '{query}':\n\n"
        + "\n".join(result_texts)
    )


@tool(parse_docstring=True)
def think_tool(reflection: str) -> str:
    """Tool for strategic reflection on research progress and decision-making.

    Use this tool after each search to analyze results and plan next steps
    systematically. Creates a deliberate pause in the research workflow.

    When to use:
    - After receiving search results: What key information did I find?
    - Before deciding next steps: Do I have enough to answer comprehensively?
    - When assessing research gaps: What specific information am I still missing?
    - Before concluding research: Can I provide a complete answer now?

    Reflection should address:
    1. Analysis of current findings — What concrete information have I gathered?
    2. Gap assessment — What crucial information is still missing?
    3. Quality evaluation — Do I have sufficient evidence for a good answer?
    4. Strategic decision — Should I continue searching or provide my answer?

    Args:
        reflection: Detailed reflection on research progress, findings, gaps,
            and next steps.

    Returns:
        Confirmation that reflection was recorded for decision-making.
    """
    return f"Reflection recorded: {reflection}"


# ===========================================================================
# 本地知识检索工具
# ---------------------------------------------------------------------------
# 默认检索根目录，可用环境变量 NOTES_ROOT 覆盖。
# 与 tavily_search 的分工：Tavily 管开放网络，这两个管用户自己的笔记与文档。
# ===========================================================================

NOTES_ROOT_ENV = "NOTES_ROOT"
NOTES_ROOT_DEFAULT = r"D:\Works\DeepAgents学习"

# agent.py 把笔记目录挂到 CompositeBackend 的 /notes/ 前缀下，
# 这里同步导出挂载点，供提示词与工具描述引用，避免两边各写一份字符串。
NOTES_MOUNT = "/notes/"

# 超时保护：deepagents 的文件工具扫到超大目录会超时并返回 partial 结果
# （实测 root_dir 指向仓库根时 grep 15s 超时）。工具目录必须收窄。
# 遍历时跳过的目录名（精确匹配，不做递归猜测）
SKIP_DIR_NAMES = {
    ".venv",
    ".git",
    ".cache",
    ".idea",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
}

# 这些后缀的目录一律跳过（构建产物，不是笔记）
SKIP_DIR_SUFFIXES = (".egg-info",)

# 只把这些后缀当文本读
TEXT_SUFFIXES = {".md", ".txt", ".rst"}

# 单文件读取上限，防止误吞大文件把上下文撑爆
MAX_FILE_BYTES = 2_000_000

# 命中行的展示宽度
SNIPPET_WIDTH = 180


def _resolve_notes_root(root_dir: str | None = None) -> Path:
    """把 root_dir / 环境变量 / 内置默认值解析成一个绝对路径。"""
    raw = (root_dir or "").strip() or os.getenv(NOTES_ROOT_ENV) or NOTES_ROOT_DEFAULT
    return Path(raw).expanduser().resolve()


def _iter_note_files(root: Path):
    """深度优先遍历 root，产出所有可当文本读的文件路径。"""
    for dirpath, dirnames, filenames in os.walk(root):
        # 就地裁剪 dirnames，跳过依赖目录、隐藏目录与构建产物
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SKIP_DIR_NAMES
            and not d.startswith(".")
            and not d.endswith(SKIP_DIR_SUFFIXES)
        )
        for name in sorted(filenames):
            if Path(name).suffix.lower() in TEXT_SUFFIXES:
                yield Path(dirpath) / name


def _read_note_text(path: Path) -> str:
    """读文件，编码异常不炸（坏字节替换掉即可）。"""
    if path.stat().st_size > MAX_FILE_BYTES:
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


@tool(parse_docstring=True)
def search_local_notes(
    query: str,
    max_results: Annotated[int, InjectedToolArg] = 5,
) -> str:
    """Search local Markdown notes for a keyword and return cited snippets.

    Use this when the answer may already live in the user's own notes or
    project docs, as opposed to needing a live web lookup. It is free,
    offline, and returns exact file paths plus line numbers so findings
    can be cited.

    Args:
        query: Keyword or short phrase to look for. Matching is
            case-insensitive and checks both file names and file bodies.
        max_results: Maximum number of matching files to report.

    Returns:
        Markdown-formatted hits grouped by file, each line prefixed with
        its line number, plus a summary of how many files were scanned.
    """
    if not query or not query.strip():
        return "Error: query must be a non-empty string."

    root = _resolve_notes_root()
    if not root.is_dir():
        return (
            f"Error: notes root not found: {root}\n"
            f"Set the {NOTES_ROOT_ENV} environment variable to a valid directory."
        )

    needle = query.strip().lower()
    hits: list[tuple[int, Path, list[tuple[int, str]]]] = []
    scanned = 0

    for path in _iter_note_files(root):
        scanned += 1
        try:
            text = _read_note_text(path)
        except OSError:
            continue

        rel = path.relative_to(root)
        # 文件名命中权重最高：直接指向「这篇讲的就是它」
        score = 5 if needle in path.name.lower() else 0

        matched: list[tuple[int, str]] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                score += 1
                matched.append((lineno, line.strip()[:SNIPPET_WIDTH]))

        if score:
            hits.append((score, rel, matched))

    if not hits:
        return (
            f"No local notes matched '{query}'.\n"
            f"Scanned {scanned} file(s) under {root}.\n"
            "Consider falling back to tavily_search for a live web lookup."
        )

    hits.sort(key=lambda item: item[0], reverse=True)
    total_lines = sum(len(m) for _, _, m in hits)

    parts = [
        f"Local notes: '{query}' — {len(hits)} file(s) matched, "
        f"{total_lines} line(s), {scanned} file(s) scanned under {root}.",
        "",
    ]
    for _, rel, matched in hits[: max(1, max_results)]:
        parts.append(f"### {rel.as_posix()}  ({len(matched)} hit(s))")
        for lineno, snippet in matched[:12]:
            parts.append(f"- L{lineno}: {snippet}")
        parts.append("")

    parts.append('Open a file in full with read_local_note(path="<path above>").')
    return "\n".join(parts)


@tool(parse_docstring=True)
def read_local_note(
    path: str,
    start_line: int = 1,
    max_lines: int = 150,
) -> str:
    """Read a real file from the local notes root, with line slicing.

    Use this after search_local_notes to open a specific hit. Note that the
    agent's built-in read_file tool works on a virtual filesystem stored in
    graph state and cannot reach files on disk, so local reads go here.

    Args:
        path: Absolute path, or a path relative to the notes root
            (for example "Learn-Notes/Task1-AgentSeek环境搭建与踩坑.md").
        start_line: 1-based line number to start reading from.
        max_lines: Maximum number of lines to return.

    Returns:
        The file content with line numbers, or an error string on failure.
    """
    if not path or not path.strip():
        return "Error: path must be a non-empty string."

    root = _resolve_notes_root()
    candidate = Path(path.strip())
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()

    # 访问边界：只允许读 notes root 以内的文件
    try:
        target.relative_to(root)
    except ValueError:
        return (
            f"Error: access denied — {target} is outside the notes root {root}.\n"
            f"Set {NOTES_ROOT_ENV} if you really need a wider scope."
        )

    if not target.is_file():
        return f"Error: file not found: {target}"

    try:
        lines = _read_note_text(target).splitlines()
    except OSError as exc:
        return f"Error reading {target}: {exc!s}"

    total = len(lines)
    start = max(1, int(start_line))
    count = max(1, int(max_lines))
    window = lines[start - 1 : start - 1 + count]

    if not window:
        return f"Error: start_line={start} is past the end of {target.name} ({total} lines)."

    body = "\n".join(f"{start + i:>5}| {line}" for i, line in enumerate(window))
    end = start + len(window) - 1
    return (
        f"{target.as_posix()}  (lines {start}-{end} of {total})\n"
        f"{'-' * 60}\n{body}"
    )

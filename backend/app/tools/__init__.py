from app.tools.base import Tool, ToolContext, ToolOutput, ToolRegistry


def default_registry() -> ToolRegistry:
    from app import rag
    from app.tools import python_exec, web

    return ToolRegistry([web.SEARCH_TOOL, web.FETCH_TOOL, python_exec.TOOL, rag.TOOL])


__all__ = ["Tool", "ToolContext", "ToolOutput", "ToolRegistry", "default_registry"]

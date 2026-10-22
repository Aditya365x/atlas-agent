from app.tools.base import Tool, ToolContext, ToolOutput, ToolRegistry


def default_registry() -> ToolRegistry:
    from app import rag
    from app.tools import python_exec, web

    return ToolRegistry([web.SEARCH_TOOL, web.FETCH_TOOL, python_exec.TOOL, rag.TOOL])


async def build_registry() -> ToolRegistry:
    """Built-in tools plus any tools discovered on configured MCP servers."""
    from app.config import get_settings

    registry = default_registry()
    servers = get_settings().mcp_servers
    if servers:
        from app.tools.mcp_client import load_mcp_tools

        registry = ToolRegistry([*registry.tools(), *await load_mcp_tools(servers)])
    return registry


__all__ = ["Tool", "ToolContext", "ToolOutput", "ToolRegistry", "build_registry", "default_registry"]

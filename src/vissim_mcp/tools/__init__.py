"""MCP tool modules (PLAN 7).

Every module exposes ``register(mcp, service) -> None`` and registers its tools through
``tools._common.register_tool``, which gives all tools the same response format and error handling.
``server.build_server`` imports and registers the modules.
"""

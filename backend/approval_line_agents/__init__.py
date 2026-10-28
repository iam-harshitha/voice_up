"""Agent-side integrations for Approval Line (Chunk 4).

These run outside the API process and talk to it only through its authenticated REST
API, with their own least-privilege API key:

- ``client``: a small, strict async REST client shared by the MCP server and demo agent.
- ``mcp_server``: an MCP server exposing Approval Line tools to MCP-capable agents.
- ``demo``: a demo agent that asks for authorization and runs a harmless simulated
  deployment only when the backend reports ``authorized: true``.

Nothing here computes a decision: the backend's policy engine is the only authority.
"""

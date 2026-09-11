"""folio - a command-line client for Wealthfolio's AI Agent Access server.

Talks to the same MCP endpoint the desktop app (or a self-hosted server)
exposes to AI agents, so every call goes through Wealthfolio's own services,
scope checks and audit log - never the SQLite file directly.
"""
__version__ = '0.1.0'

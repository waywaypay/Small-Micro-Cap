"""Local stdio MCP server that wraps the deployed landmine-api service.

Exposes two tools — ``run_landmine`` and ``run_universe`` — that call the
FastAPI service (see ``landmine_api/``) over HTTP and return its scorecard JSON.
The HTTP endpoint and key come from ``LANDMINE_API_URL`` / ``LANDMINE_API_KEY``.

The server lives in ``landmine_mcp.server`` and is intentionally not imported
here, so ``python -m landmine_mcp.server`` runs cleanly without a re-import.
"""

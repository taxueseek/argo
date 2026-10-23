"""Console entrypoint for the packaged Argo MCP server."""

from __future__ import annotations

from .cli import _run


def main() -> None:
    _run("scripts/mcp_server.py")


if __name__ == "__main__":
    main()

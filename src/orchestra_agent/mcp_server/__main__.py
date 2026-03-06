from __future__ import annotations

import argparse

from orchestra_agent.mcp_server import run_mcp_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run orchestra-agent MCP server (stdio).")
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace root for file tools.",
    )
    parser.add_argument(
        "--name",
        default="orchestra-workspace",
        help="MCP server name.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    run_mcp_server(workspace_root=args.workspace, server_name=args.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

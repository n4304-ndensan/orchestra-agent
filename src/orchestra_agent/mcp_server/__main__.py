from __future__ import annotations

import argparse

from orchestra_agent.config import AppConfig, load_app_config, resolve_config_path
from orchestra_agent.mcp_server import run_jsonrpc_server, run_mcp_server


def build_parser(config: AppConfig | None = None) -> argparse.ArgumentParser:
    defaults = config or AppConfig()
    parser = argparse.ArgumentParser(description="Run orchestra-agent MCP server.")
    parser.add_argument(
        "--config",
        default=str(config.source_path) if config and config.source_path is not None else None,
        help="Path to orchestra-agent TOML config file.",
    )
    parser.add_argument(
        "--workspace",
        default=defaults.workspace.root,
        help="Workspace root for file and Excel tools.",
    )
    parser.add_argument(
        "--name",
        default=defaults.mcp.name,
        help="MCP server name.",
    )
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=defaults.mcp.transport,
        help="Server transport. HTTP serves JSON-RPC for orchestra-agent CLI/API integration.",
    )
    parser.add_argument(
        "--host",
        default=defaults.mcp.host,
        help="HTTP host when --transport http.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=defaults.mcp.port,
        help="HTTP port when --transport http.",
    )
    parser.add_argument(
        "--path",
        default=defaults.mcp.path,
        help="HTTP JSON-RPC path when --transport http.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    config_path = resolve_config_path(argv)
    config = load_app_config(config_path)
    parser = build_parser(config)
    args = parser.parse_args(argv)
    workspace = config.resolve_workspace(args.workspace)
    if args.transport == "stdio":
        run_mcp_server(workspace_root=workspace, server_name=args.name)
        return 0
    run_jsonrpc_server(
        workspace_root=workspace,
        host=args.host,
        port=args.port,
        path=args.path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse

from orchestra_agent.config import AppConfig, load_app_config, resolve_config_path
from orchestra_agent.mcp_server import run_jsonrpc_server, run_mcp_server
from orchestra_agent.mcp_server.logging_utils import (
    configure_mcp_logging,
    get_mcp_logger,
    log_event,
)

logger = get_mcp_logger(__name__)


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
        "--server",
        default=None,
        help="Named mcp.servers profile from the TOML config.",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="MCP server name.",
    )
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=None,
        help="Server transport. HTTP serves JSON-RPC for orchestra-agent CLI/API integration.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="HTTP host when --transport http.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP port when --transport http.",
    )
    parser.add_argument(
        "--path",
        default=None,
        help="HTTP JSON-RPC path when --transport http.",
    )
    parser.add_argument(
        "--tool-group",
        choices=["all", "files", "excel"],
        default=None,
        help="Expose only a subset of built-in tools.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_mcp_logging()
    config_path = resolve_config_path(argv)
    config = load_app_config(config_path)
    parser = build_parser(config)
    args = parser.parse_args(argv)
    try:
        server_settings = config.mcp.resolve_server(args.server)
    except (KeyError, ValueError) as exc:
        parser.error(str(exc))
    workspace = config.resolve_workspace(args.workspace)
    server_name = args.name or server_settings.name
    transport = args.transport or server_settings.transport
    host = args.host or server_settings.host
    port = args.port if args.port is not None else server_settings.port
    path = args.path or server_settings.path
    tool_group = args.tool_group or server_settings.tool_group
    log_event(
        logger,
        "mcp_server_launch_config",
        config_path=config.source_path,
        workspace=workspace,
        server_name=server_name,
        transport=transport,
        host=host,
        port=port,
        path=path,
        tool_group=tool_group,
    )
    if transport == "stdio":
        run_mcp_server(
            workspace_root=workspace,
            server_name=server_name,
            tool_group=tool_group,
        )
        return 0
    run_jsonrpc_server(
        workspace_root=workspace,
        host=host,
        port=port,
        path=path,
        tool_group=tool_group,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

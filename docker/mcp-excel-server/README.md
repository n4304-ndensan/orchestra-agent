# Excel MCP Server

Standalone Docker assets for the `excel` MCP server live here.

The server now exposes the safe edit-session toolset for local workbooks:

- `list_sources`
- `find_workbooks`
- `resolve_workbook`
- `inspect_workbook`
- `read_range`
- `read_table`
- `open_edit_session`
- `stage_update_cells`
- `stage_append_rows`
- `stage_create_sheet`
- `preview_edit_session`
- `validate_edit_session`
- `commit_edit_session`
- `cancel_edit_session`
- `list_backups`
- `restore_backup`

Legacy compatibility tools such as `excel.open_file` and `excel.write_cells` remain available.

## Run

From the repository root:

```powershell
docker compose -f docker/mcp-excel-server/docker-compose.yml up --build -d
```

Health check:

- `http://127.0.0.1:8020/health`

Optional server config:

- TOML: `docker/mcp-excel-server/config.example.toml`
- YAML: `docker/mcp-excel-server/config.example.yaml`

Set `EXCEL_MCP_CONFIG` to one of those files or to your own config path.

Stop:

```powershell
docker compose -f docker/mcp-excel-server/docker-compose.yml down
```

## Create another isolated server from this folder

1. Copy `docker/mcp-excel-server/` to a new directory.
2. Update `Dockerfile` so `--server` matches the new `[[mcp.servers]].name`.
3. Update `docker-compose.yml`:
   - service name
   - `container_name`
   - published port
   - healthcheck port
4. Add the new server entry to `orchestra-agent.toml`.

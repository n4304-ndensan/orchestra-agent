# Docker Layout

MCP server containers are split into fully independent directories:

- `docker/mcp-files-server/`
- `docker/mcp-excel-server/`

Each directory owns its own `Dockerfile`, standalone `docker-compose.yml`, and README.

## Run only one MCP server

Files MCP:

```powershell
docker compose -f docker/mcp-files-server/docker-compose.yml up --build -d
```

Excel MCP:

```powershell
docker compose -f docker/mcp-excel-server/docker-compose.yml up --build -d
```

## Add another isolated MCP server

If you want a new independent MCP server container:

1. Copy the closer directory under `docker/`.
2. Update the copied `Dockerfile` so `--server` points at the new server name.
3. Update the copied `docker-compose.yml`:
   - service name
   - `container_name`
   - exposed port
   - healthcheck port
4. Add a matching `[[mcp.servers]]` entry in `orchestra-agent.toml`.
5. If the full stack should start it, add the service to the root `docker-compose.yml`.

If the server needs a brand new tool group implementation, also wire it in:

1. `src/orchestra_agent/mcp_server/server.py`
2. `src/orchestra_agent/mcp_server/__main__.py`
3. `tests/unit/mcp_server/`

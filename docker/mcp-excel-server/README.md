# Excel MCP Server

Standalone Docker assets for the `excel` MCP server live here.

## Run

From the repository root:

```powershell
docker compose -f docker/mcp-excel-server/docker-compose.yml up --build -d
```

Health check:

- `http://127.0.0.1:8020/health`

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

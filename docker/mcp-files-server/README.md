# Files MCP Server

Standalone Docker assets for the `files` MCP server live here.

## Run

From the repository root:

```powershell
docker compose -f docker/mcp-files-server/docker-compose.yml up --build -d
```

Health check:

- `http://127.0.0.1:8010/health`

Stop:

```powershell
docker compose -f docker/mcp-files-server/docker-compose.yml down
```

## Create another isolated server from this folder

1. Copy `docker/mcp-files-server/` to a new directory.
2. Update `Dockerfile` so `--server` matches the new `[[mcp.servers]].name`.
3. Update `docker-compose.yml`:
   - service name
   - `container_name`
   - published port
   - healthcheck port
4. Add the new server entry to `orchestra-agent.toml`.

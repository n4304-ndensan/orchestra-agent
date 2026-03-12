# Files MCP Server

Standalone Docker assets for the `files` MCP server live here.

The current build exposes both:

- legacy compatibility tools such as `fs_list_entries` and `fs_copy_file`
- safe v1 tools under the `file.*` namespace such as `file.find_items`, `file.read_text`, and `file.open_text_edit_session`

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

## Config samples

- `docker/mcp-files-server/config.example.toml`
- `docker/mcp-files-server/config.example.yaml`

Both samples are local-first. When `FILE_MCP_REMOTE_ENABLED=true` and Graph credentials are supplied, the safe `file.*` tools can resolve, search, read, and round-trip edit SharePoint / OneDrive text files. Structural remote operations still remain intentionally limited.

## Create another isolated server from this folder

1. Copy `docker/mcp-files-server/` to a new directory.
2. Update `Dockerfile` so `--server` matches the new `[[mcp.servers]].name`.
3. Update `docker-compose.yml`:
   - service name
   - `container_name`
   - published port
   - healthcheck port
4. Add the new server entry to `orchestra-agent.toml`.

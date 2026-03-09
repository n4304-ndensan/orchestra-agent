# orchestra-agent

Workflow-driven orchestration control plane for Excel automation via HTTP JSON-RPC MCP servers.

資料:
- [Current Status and Flow](docs/current-status.md)

## Purpose

`orchestra-agent` manages:

- workflow creation
- step-plan compilation
- policy evaluation and approval gating
- snapshot-before-mutation
- execution orchestration via MCP
- failure recovery with feedback and replanning
- audit logging and run-state tracking
- HTTP control plane API

The agent does not execute Excel operations directly. It delegates tool calls to external MCP servers.

## Implemented architecture

- `domain/`: `Workflow`, `Step`, `StepPlan`, `ExecutionRecord`, `AgentState`, enums
- `ports/`: planner, policy, MCP client, snapshot, state store, repositories, audit logger
- `adapters/`
  - `planner/llm_planner.py`
  - `policy/default_policy_engine.py`
  - `mcp/jsonrpc_mcp_client.py`
  - `snapshot/filesystem_snapshot_manager.py`
  - `db/filesystem_agent_state_store.py`
  - `db/filesystem_audit_logger.py`
- `executor/`
  - `plan_executor.py`
  - `failure_handler.py`
- `application/use_cases/`
  - create workflow
  - compile plan
  - evaluate policy
  - approve plan
  - execute plan
  - handle failure
  - apply feedback
- `api/`
  - `workflow_api.py`
  - `approval_api.py`
  - `run_api.py`
- `control_plane.py`
- `mcp_server/`
  - `jsonrpc_server.py`
  - `excel_service.py`

## Excel automation flow

Input example:

`Open Excel file sales.xlsx, calculate totals for column C, create a summary sheet, and export as summary.xlsx`

Generated plan (typical):

1. `excel.open_file`
2. `excel.read_sheet`
3. `excel.calculate_sum`
4. `excel.create_sheet`
5. `excel.write_cells`
6. `excel.save_file`

Execution behavior:

- dependency-aware (DAG)
- skip/run flags respected
- mandatory approval before every step execution
- mandatory review approval after every step execution (approve or feedback)
- snapshot before mutating steps
- failure/feedback pipeline: restore -> log -> workflow XML feedback update -> replan -> approval -> resume
- final completion locks workflow and step plan artifacts

## Quick start

### Config-first deployment

All runtime settings can be managed from a single TOML file:

- [orchestra-agent.toml](orchestra-agent.toml)
- API keys stay in environment variables only
- `OPENAI_API_KEY`
- `GEMINI_API_KEY`

For Docker Compose, copy `.env.example` to `.env` and set keys only when needed.

Bring up the full product stack:

```powershell
docker compose up --build
```

Services:

- MCP server: `http://127.0.0.1:8000/health`
- Control plane API: `http://127.0.0.1:9000/health`

Run the one-shot CLI in Docker:

```powershell
docker compose run --rm orchestra-cli "sales.xlsxのC列を集計してsummary.xlsxへ"
```

### Direct local startup

Recommended: run with the built-in HTTP Excel MCP server.

```powershell
uv run --extra mcp-server orchestra-agent-mcp --config .\orchestra-agent.toml
```

Then execute the workflow through the CLI:

```powershell
uv run orchestra-agent --config .\orchestra-agent.toml "sales.xlsxのC列を集計してsummary.xlsxへ" --mcp-endpoint http://127.0.0.1:8000/mcp
```

Run the control plane API:

```powershell
uv run orchestra-agent-api --config .\orchestra-agent.toml --mcp-endpoint http://127.0.0.1:8000/mcp
```

Fast local verification without an external MCP server is still available in mock mode:

```powershell
uv run orchestra-agent --config .\orchestra-agent.toml "sales.xlsxのC列を集計してsummary.xlsxへ" --mcp-endpoint ""
```

Built-in MCP server transports:

```powershell
uv run --extra mcp-server orchestra-agent-mcp --workspace . --transport http
uv run --extra mcp-server orchestra-agent-mcp --workspace . --transport stdio
```

Workflow / plan / state / audit 保存:

- config の `workspace.root = "./workspace"` を起点に保存
- workflow は `workspace/workflow/<workflow_id>/workflow.xml`
- step plan は `workspace/plan/<workflow_id>/<step_plan_id>/step_plan_v{n}.json`
- run state は `workspace/.orchestra_state/runs/<run_id>.json`
- audit log は `workspace/.orchestra_state/audit/events.ndjson`
- snapshots は `workspace/.orchestra_snapshots/`

既存workflowを指定して実行:

```powershell
uv run orchestra-agent --config .\orchestra-agent.toml --workflow-id wf-sales-summary
```

workflow XMLをインポートして実行:

```powershell
uv run orchestra-agent --config .\orchestra-agent.toml --workflow-xml .\workflow_source.xml
```

workflow IDを固定して新規作成＋実行:

```powershell
uv run orchestra-agent --config .\orchestra-agent.toml "sales.xlsxのC列を集計してsummary.xlsxへ" --workflow-id wf-sales-summary
```

### Control plane API examples

Create a workflow:

```powershell
curl -X POST http://127.0.0.1:9000/workflows `
  -H "Content-Type: application/json" `
  -d '{"name":"Excel summary","objective":"sales.xlsxのC列を集計してsummary.xlsxへ"}'
```

Generate a plan:

```powershell
curl -X POST http://127.0.0.1:9000/workflows/wf-sales-summary/plans -H "Content-Type: application/json" -d "{}"
```

Start and resume a run:

```powershell
curl -X POST http://127.0.0.1:9000/runs `
  -H "Content-Type: application/json" `
  -d '{"workflow_id":"wf-sales-summary","step_plan_id":"sp-...","run_id":"run-1","approved":false}'

curl -X POST http://127.0.0.1:9000/runs/run-1/approval `
  -H "Content-Type: application/json" `
  -d '{"approve":true}'
```

### Safe LLM augmentation

Current planner is deterministic by default.
You can safely augment it by giving a proposal patch file:

```powershell
uv run orchestra-agent --config .\orchestra-agent.toml "sales.xlsxのC列を集計してsummary.xlsxへ" --llm-proposal-file llm_patch.json
```

`llm_patch.json` example:

```json
{
  "steps": [
    {
      "step_id": "calculate_totals",
      "resolved_input": {
        "column": "D"
      }
    }
  ]
}
```

Safety properties:
- starts from deterministic draft plan
- proposal may patch existing steps only
- tool refs are restricted to Excel allow-list
- final plan must still pass domain validation (including DAG)
- invalid proposal is rejected and deterministic plan is used

### Live OpenAI integration

Enable live LLM proposal generation:

```powershell
$env:OPENAI_API_KEY="your_api_key"
uv run orchestra-agent --config .\orchestra-agent.toml "sales.xlsxのC列を集計してsummary.xlsxへ" --llm-provider openai --llm-openai-model gpt-4.1-mini
```

Notes:
- OpenAI is used only for proposal generation (patches).
- The final plan still goes through strict safety validation.
- If OpenAI output is malformed or unsafe, the planner falls back to deterministic mode.

### Live Google Gemini Developer API integration

Enable live LLM proposal generation with the Gemini Developer API:

```powershell
$env:GEMINI_API_KEY="your_api_key"
uv run orchestra-agent --config .\orchestra-agent.toml "sales.xlsxのC列を集計してsummary.xlsxへ" --llm-provider google --llm-google-model gemini-2.5-flash
```

Notes:
- `GEMINI_API_KEY` is used by default, and `GOOGLE_API_KEY` is also accepted as a fallback.
- Gemini is used only for proposal generation (patches).
- The final plan still goes through strict safety validation.
- If Gemini output is malformed or unsafe, the planner falls back to deterministic mode.

## Development

Install and run tests:

```powershell
$env:UV_CACHE_DIR=".uv-cache"
$env:UV_PYTHON_INSTALL_DIR=".uv-python"
$env:UV_PROJECT_ENVIRONMENT=".venv-uv"
uv run --python 3.13 --extra mcp-server pytest -q tests -o cache_dir=.pytest_cache_local
```

Lint:

```powershell
$env:UV_CACHE_DIR=".uv-cache"
$env:UV_PYTHON_INSTALL_DIR=".uv-python"
$env:UV_PROJECT_ENVIRONMENT=".venv-uv"
uv run --python 3.13 ruff check src tests
```

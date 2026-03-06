# orchestra-agent

Workflow-driven orchestration control plane for Excel automation via MCP servers.

## Purpose

`orchestra-agent` manages:

- workflow creation
- step-plan compilation
- policy evaluation and approval gating
- snapshot-before-mutation
- execution orchestration via MCP
- failure recovery with feedback and replanning
- audit logging and run-state tracking

The agent does not execute Excel operations directly. It delegates tool calls to external MCP servers.

## Implemented architecture

- `domain/`: `Workflow`, `Step`, `StepPlan`, `ExecutionRecord`, `AgentState`, enums
- `ports/`: planner, policy, MCP client, snapshot, state store, repositories, audit logger
- `adapters/`
  - `planner/llm_planner.py`
  - `policy/default_policy_engine.py`
  - `mcp/jsonrpc_mcp_client.py`
  - `snapshot/filesystem_snapshot_manager.py`
  - `db/postgres_agent_state_store.py` (in-memory compatible adapter)
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
- approval wait on protected steps
- snapshot before mutating steps
- failure pipeline: restore -> log -> feedback -> replan -> approval -> resume

## Development

Install and run tests:

```powershell
$env:UV_CACHE_DIR=".uv-cache"
$env:UV_PYTHON_INSTALL_DIR=".uv-python"
$env:UV_PROJECT_ENVIRONMENT=".venv-uv"
uv run --python 3.13 pytest -q tests -o cache_dir=.pytest_cache_local
```

Lint:

```powershell
$env:UV_CACHE_DIR=".uv-cache"
$env:UV_PYTHON_INSTALL_DIR=".uv-python"
$env:UV_PROJECT_ENVIRONMENT=".venv-uv"
uv run --python 3.13 ruff check src tests
```

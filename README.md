# orchestra-agent

Workflow-driven orchestration control plane for Excel automation via MCP servers.

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
- mandatory approval before every step execution
- mandatory review approval after every step execution (approve or feedback)
- snapshot before mutating steps
- failure/feedback pipeline: restore -> log -> workflow XML feedback update -> replan -> approval -> resume
- final completion locks workflow and step plan artifacts

## Quick start

Run from one command (mock MCP mode):

```powershell
uv run python main.py "sales.xlsxのC列を集計してsummary.xlsxへ"
```

For real MCP execution:

```powershell
uv run python main.py "sales.xlsxのC列を集計してsummary.xlsxへ" --mcp-endpoint http://localhost:8000/mcp
```

MCP server scaffold (stdio, file tools):

```powershell
pip install ".[mcp-server]"
uv run python -m orchestra_agent.mcp_server --workspace .
```

Workflow XML / Plan 保存:

- workflow は `workflow/<workflow_id>/workflow.xml` で管理
- workflow version は `workflow/<workflow_id>/versions/workflow_v{n}.xml`
- feedback は `workflow/<workflow_id>/feedback/feedback_v{n}.txt`
- step plan は `plan/<workflow_id>/<step_plan_id>/step_plan_v{n}.json`

既存workflowを指定して実行:

```powershell
uv run python main.py --workflow-id wf-sales-summary --workspace .
```

workflow XMLをインポートして実行:

```powershell
uv run python main.py --workflow-xml .\workflow_source.xml --workspace .
```

workflow IDを固定して新規作成＋実行:

```powershell
uv run python main.py "sales.xlsxのC列を集計してsummary.xlsxへ" --workflow-id wf-sales-summary --workspace .
```

### Safe LLM augmentation

Current planner is deterministic by default.
You can safely augment it by giving a proposal patch file:

```powershell
uv run python main.py "sales.xlsxのC列を集計してsummary.xlsxへ" --llm-proposal-file llm_patch.json
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
uv run python main.py "sales.xlsxのC列を集計してsummary.xlsxへ" --llm-provider openai --llm-openai-model gpt-4.1-mini
```

Notes:
- OpenAI is used only for proposal generation (patches).
- The final plan still goes through strict safety validation.
- If OpenAI output is malformed or unsafe, the planner falls back to deterministic mode.

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

# orchestra-agent

Workflow-driven orchestration control plane for AI-assisted automation via HTTP JSON-RPC MCP servers.

資料:
- [Current Status and Flow](docs/current-status.md)

## Purpose

`orchestra-agent` manages:

- workflow creation
- MCP-backed tool discovery and execution
- multi-file references for LLM requests
- practical file and Excel inspection tools for real operations
- step-plan compilation
- policy evaluation and approval gating
- pre-step snapshots for comparison and rollback
- execution orchestration across one or more MCP servers
- failure recovery with feedback and replanning
- audit logging and run-state tracking
- HTTP control plane API

Excel automation is one bundled capability, not the whole product. The core design is "turn any procedure into a governed workflow", then execute approved steps through MCP tools.

Execution boundary:

- With a live LLM provider, AI plans the workflow and also orchestrates each step against the MCP runtime.
- MCP servers remain the execution substrate that actually touches files, Excel workbooks, and other local systems.
- The runtime enforces approval gates, pre-step snapshots, audit logs, and recovery around those AI decisions.
- Built-in MCP roles are currently `files` and `excel`, and the runtime can aggregate additional JSON-RPC MCP servers.

`llm.provider = openai` or `google` enables the full AI-controlled mode. `none` keeps a deterministic local-safe profile.

## Implemented architecture

- `domain/`: `Workflow`, `Step`, `StepPlan`, `ExecutionRecord`, `AgentState`, enums
- `ports/`: planner, policy, MCP client, snapshot, state store, repositories, audit logger
- `adapters/`
  - `planner/llm_planner.py`
  - `policy/default_policy_engine.py`
  - `mcp/jsonrpc_mcp_client.py`
  - `mcp/multi_endpoint_mcp_client.py`
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
  - `file_service.py`
  - `excel_service.py`

## Automation flow

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
- plan approval only when the plan contains approval-gated steps
- pre/post step approval only for high-risk steps or steps with `requires_approval=true`
- pre-step snapshot before every executed step
- failure/feedback pipeline: restore -> log -> workflow XML feedback update -> AI replan with
  source workflow document + source step-plan document + correction summary -> approval -> resume
- final completion locks workflow and step plan artifacts

With a live LLM provider, step execution runs through an AI-controlled MCP runtime. The model
receives the step description, planned tool, resolved input, dependency results, and the current
MCP tool catalog with descriptions. It can call one or more MCP tools, request real file
attachments, write workspace files, and then set the final step result.

During agentic execution, the model first sees a workspace file index. If it decides it needs one
or more real files, it can return a `request_file_attachments` action, and the executor will
re-query the LLM with those files attached to the message.

The default deterministic draft planner is still optimized for the bundled Excel profile. When a
live LLM planner is enabled, planning becomes tool-aware across all discovered MCP servers.

Control-flow note:

- `StepPlan` is still a static DAG. First-class `if` / `for` syntax is not modeled yet.
- Branching, iterative search, and ambiguous procedural work should run inside
  `orchestra.llm_execute` or `orchestra.ai_review`.
- In that mode, the AI can loop over MCP tools, inspect intermediate results, and decide the next
  call while the runtime still enforces approval, snapshots, audit logs, and recovery.

Practical bundled MCP tools:

- `files`
  - `fs_list_entries`
  - `fs_find_entries`
  - `fs_grep_text`
  - `fs_read_text`
  - `fs_write_text`
- `excel`
  - `excel.open_file`
  - `excel.read_sheet`
  - `excel.read_cells`
  - `excel.grep_cells`
  - `excel.calculate_sum`
  - `excel.create_sheet`
  - `excel.write_cells`
  - `excel.list_images`
  - `excel.extract_image`
  - `excel.save_file`
- built-in AI tools
  - `orchestra.llm_execute`
  - `orchestra.ai_review`

`orchestra.ai_review` is for steps that need human-brain-style judgment such as review, comparison,
triage, or "read these files and tell me whether this procedure/program is acceptable". The model
can inspect attached files and workspace files, produce a structured result, and later steps can use
that result to decide what to write or execute through MCP.

Replanning behavior:

- When failure or human feedback triggers a replan, the runtime builds a structured `replan_context`.
- That context contains the source workflow document, the source step-plan document, and the change
  summary that must be applied.
- `StructuredLlmPlanner` and `LlmStepProposalProvider` both forward that context to the model, so
  the AI replans against the original document plus the requested correction instead of only reading
  a flat feedback string.

Refactoring direction:

- `Workflow` / `StepPlan` document rendering is now centralized in pure domain serialization helpers.
- Planner payload generation and replan document generation reuse that shared serialization instead
  of duplicating XML / JSON assembly in multiple adapters.
- Runtime approval rules are now aligned with `risk_level` and `requires_approval`, so the step
  schema matches actual execution semantics.

## Quick start

### Config-first deployment

All runtime settings can be managed from a single TOML file:

- [orchestra-agent.toml](orchestra-agent.toml)
- add more MCP runtimes by appending more `[[mcp.servers]]` blocks
- API keys stay in environment variables only
- `OPENAI_API_KEY`
- `GEMINI_API_KEY`

For Docker Compose, copy `.env.example` to `.env` and set keys only when needed.

Bring up the full product stack:

```powershell
docker compose up --build
```

Services:

- Files MCP server: `http://127.0.0.1:8010/health`
- Excel MCP server: `http://127.0.0.1:8020/health`
- Control plane API: `http://127.0.0.1:9000/health`
- Tool catalog: `http://127.0.0.1:9000/tools`

Run the one-shot CLI in Docker:

```powershell
docker compose run --rm orchestra-cli "sales.xlsxのC列を集計してsummary.xlsxへ"
```

### Direct local startup

Recommended local topology: run the built-in MCP roles separately and let the runtime aggregate them.

```powershell
uv run --extra mcp-server orchestra-agent-mcp --config .\orchestra-agent.toml --server files
uv run --extra mcp-server orchestra-agent-mcp --config .\orchestra-agent.toml --server excel
```

Then execute the workflow through the CLI:

```powershell
uv run orchestra-agent --config .\orchestra-agent.toml `
  "sales.xlsxのC列を集計してsummary.xlsxへ" `
  --mcp-endpoint http://127.0.0.1:8010/mcp `
  --mcp-endpoint http://127.0.0.1:8020/mcp
```

Run the control plane API:

```powershell
uv run orchestra-agent-api --config .\orchestra-agent.toml `
  --mcp-endpoint http://127.0.0.1:8010/mcp `
  --mcp-endpoint http://127.0.0.1:8020/mcp
```

Fast local verification without an external MCP server is still available in mock mode:

```powershell
uv run orchestra-agent --config .\orchestra-agent.toml `
  "sales.xlsxのC列を集計してsummary.xlsxへ" `
  --mcp-endpoint ""
```

Built-in MCP server transports:

```powershell
uv run --extra mcp-server orchestra-agent-mcp --workspace . --transport http --tool-group all
uv run --extra mcp-server orchestra-agent-mcp --workspace . --transport stdio --tool-group files
```

Workflow / plan / state / audit 保存:

- config の `workspace.root = "./workspace"` を起点に保存
- workflow は `workspace/workflow/<workflow_id>/workflow.xml`
- workflow XML には `reference_files` も保存
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

LLM step 実行時に、モデルが必要と判断したファイルを後続ターンで添付:

```powershell
uv run orchestra-agent --config .\orchestra-agent.toml `
  "sales.xlsxのC列を集計してsummary.xlsxへ" `
  --llm-provider openai
```

`orchestra.llm_execute` step では、LLM は次のような action を返せます:

```json
{
  "actions": [
    {
      "type": "request_file_attachments",
      "paths": ["specs/requirements.pdf", "specs/mapping.csv"],
      "reason": "Need the source documents"
    }
  ]
}
```

これを受けて executor は対象ファイルを添付し、同じ step を再度 LLM に問い合わせます。

`orchestra.ai_review` も同じ attachment / workspace index の仕組みを使えますが、主目的は
分析結果の返却です。たとえば「フォルダ内を調べ、Excel の特定セルに書かれた手順と program
file を照合して問題ないかレビューする」のような task は、AI が MCP tool を使って探索し、
最後に review result を返す step として実行できます。

### Control plane API examples

Create a workflow:

```powershell
curl -X POST http://127.0.0.1:9000/workflows `
  -H "Content-Type: application/json" `
  -d '{"name":"Excel summary","objective":"sales.xlsxのC列を集計してsummary.xlsxへ","reference_files":["./workspace/specs/requirements.pdf","./workspace/specs/mapping.csv"]}'
```

`reference_files` は事前ヒントとして使えますが、必須ではありません。`orchestra.llm_execute`
では workspace index を見た LLM が実行中に追加ファイルを要求できます。

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

Deterministic mode remains available for the bundled safe Excel profile.
You can also augment that draft by giving a proposal patch file:

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
- tool refs are restricted to the approved allow-list
- final plan must still pass domain validation (including DAG)
- invalid proposal is rejected and deterministic plan is used

### Live OpenAI integration

Enable live LLM planning:

```powershell
$env:OPENAI_API_KEY="your_api_key"
uv run orchestra-agent --config .\orchestra-agent.toml "sales.xlsxのC列を集計してsummary.xlsxへ" --llm-provider openai --llm-openai-model gpt-4.1-mini
```

Notes:
- Live OpenAI defaults to `planner_mode = "full"` unless overridden.
- The model can plan across all discovered MCP tools plus `orchestra.llm_execute`.
- Standard step execution is also routed through the AI MCP runtime when a live LLM is configured.
- The final plan still goes through strict safety validation.
- If OpenAI output is malformed or unsafe, the planner falls back to deterministic mode.

### Live Google Gemini Developer API integration

Enable live LLM planning with the Gemini Developer API:

```powershell
$env:GEMINI_API_KEY="your_api_key"
uv run orchestra-agent --config .\orchestra-agent.toml "sales.xlsxのC列を集計してsummary.xlsxへ" --llm-provider google --llm-google-model gemini-2.5-flash
```

Notes:
- `GEMINI_API_KEY` is used by default, and `GOOGLE_API_KEY` is also accepted as a fallback.
- Live Gemini defaults to `planner_mode = "full"` unless overridden.
- The model can plan across all discovered MCP tools plus `orchestra.llm_execute`.
- Standard step execution is also routed through the AI MCP runtime when a live LLM is configured.
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

# 🎼 orchestra-agent

AI に作業を頼むと、workflow を作って安全に実行してくれるオーケストレーターです。
Excel 操作はその一部で、`files` / `excel` ツールを使って実務フローを回せます。

- 資料: [Current Status and Flow](docs/current-status.md)
- 資料: [Workspace Artifacts Guide](docs/workspace-artifacts.md)

## ✨ これで何ができる？

- 自然言語の指示から StepPlan (DAG) を作成
- MCP ツール (`files`, `excel`) で実ファイルを操作
- 実行前後の承認ゲート
- 監査ログ、スナップショット、失敗時リプラン
- CLI / API の両方で運用

## 🧰 追加した汎用 MCP ツール

- `excel.create_file`
  - 新規 `.xlsx` を作成（例: `output/HelloWorld.xlsx`）
- `fs_copy_file`
  - workspace 内のファイルを安全にコピー

どちらも JSON-RPC MCP (`/mcp`) と FastMCP の両方で利用できます。

## 🧹 リポジトリをきれいに保つ運用

このリポジトリは、`workspace` を実行用ディレクトリとして使う前提です。
Docker 構成では全コンテナが同じ `./workspace` を共有マウントします。

- 入力: `workspace/input/...`
- 出力: `workspace/output/...`
- 実行ログ/状態: `workspace/.orchestra_state/...`
- workflow/plan: `workspace/workflow/...`, `workspace/plan/...`

ポイント:

- 仕事で触るファイルは基本 `workspace` に置く
- `src/` や設定ファイルは汚さない
- CLI/API に渡すパスは `workspace` ルート相対で書く
- 例: `input/sales.xlsx`（`workspace/input/sales.xlsx` ではない）

## 🚀 最短セットアップ (Docker)

### 1. `.env` を作る

```powershell
Copy-Item .env.example .env
```

必要なキーだけ設定:

- `OPENAI_API_KEY`
- `GEMINI_API_KEY`

### 2. workspace を作る

```powershell
New-Item -ItemType Directory -Force workspace | Out-Null
New-Item -ItemType Directory -Force workspace/input, workspace/output | Out-Null
```

### 3. 起動

```powershell
docker compose up --build -d
```

MCP サーバー単体の Docker 資産は分離しています。

- `docker/mcp-files-server/`
- `docker/mcp-excel-server/`
- 追加方法: `docker/README.md`

ヘルス確認:

- `http://127.0.0.1:8010/health` (files)
- `http://127.0.0.1:8020/health` (excel)
- `http://127.0.0.1:9000/health` (control plane)
- `http://127.0.0.1:9000/tools` (tool catalog)

## 🧪 HelloWorld レシピ (workflow 作成→実行)

### 目的

`workspace/output/HelloWorld.xlsx` を作り、セル `A1` に `HelloWorld` を入れる。

### 手順 1: workflow を作成

```powershell
curl -X POST http://127.0.0.1:9000/workflows `
  -H "Content-Type: application/json" `
  -d '{"name":"HelloWorld Excel","objective":"output/HelloWorld.xlsx を作成し、Sheet1 の A1 に HelloWorld と書き込んで保存して"}'
```

レスポンスの `workflow_id` を控えます（例: `wf-hello`）。

### 手順 2: step plan を生成

```powershell
curl -X POST http://127.0.0.1:9000/workflows/wf-hello/plans `
  -H "Content-Type: application/json" `
  -d "{}"
```

レスポンスの `step_plan_id` を控えます（例: `sp-hello`）。

### 手順 3: 実行開始

```powershell
curl -X POST http://127.0.0.1:9000/runs `
  -H "Content-Type: application/json" `
  -d '{"workflow_id":"wf-hello","step_plan_id":"sp-hello","run_id":"run-hello","approved":false}'
```

### 手順 4: 承認 (`yes/no/feedback`)

```powershell
curl -X POST http://127.0.0.1:9000/runs/run-hello/approval `
  -H "Content-Type: application/json" `
  -d '{"approve":"yes"}'
```

`"approve": true` でも同じです。

修正したい場合は `feedback` を送ります。

```powershell
curl -X POST http://127.0.0.1:9000/runs/run-hello/approval `
  -H "Content-Type: application/json" `
  -d '{"feedback":"save_file の output を output/HelloWorld.xlsx に修正して"}'
```

`feedback` を送ると workflow/step plan が再生成され、承認待ち (`PENDING`) に戻ります。

### 手順 5: 結果確認

- 出力: `workspace/output/HelloWorld.xlsx`
- 期待内容: `Sheet1!A1 = HelloWorld`

## 🧰 CLI で実行する場合

```powershell
docker compose run --rm orchestra-cli "output/HelloWorld.xlsx を作成し、Sheet1 の A1 に HelloWorld と書き込んで保存して"
```

`runtime.auto_approve = false` かつ `runtime.interactive_approval = true` の場合、
CLI は承認待ちで `yes/no/feedback` を聞きます。

## 🧭 やりたいこと別レシピ

### 1. 売上集計したい

```powershell
docker compose run --rm orchestra-cli "input/sales.xlsx の C 列を集計して output/summary.xlsx に保存"
```

### 2. API で人手承認を入れたい

- `/runs` は `approved=false` で開始
- `/runs/{run_id}/approval` に `{"approve":"yes"}` または `{"approve":"no"}`
- 間違いを修正したいときは `{"feedback":"..."}` を送る

`approve` / `approved` / `reject` は boolean と `yes/no` 文字列の両方を受け付けます。

### 3. 会社ネットワークで SSL エラーを避けたい

`orchestra-agent.toml` の `llm` セクションで CA バンドルを指定できます。

```toml
[llm]
provider = "google"
tls_verify = true
tls_ca_bundle = "./certs/company.crt"
```

CLI/API での上書きも可能です。

```powershell
uv run orchestra-agent --config .\orchestra-agent.toml `
  --llm-provider google `
  --llm-tls-ca-bundle .\certs\company.crt `
  --llm-tls-verify `
  "output/HelloWorld.xlsx を作成し、Sheet1 の A1 に HelloWorld と書き込んで保存して"
```

## ⚙️ 設定の要点 (`orchestra-agent.toml`)

### `workspace`

- `root`: 実行ベースディレクトリ
- `workflow_root`, `plan_root`: ワークフローとプラン保存先
- `state_root`, `audit_root`: 実行状態と監査ログ

### `llm`

- `provider`: `none` / `openai` / `google`
- `planner_mode`: `deterministic` / `augmented` / `full`
- `tls_verify`: TLS検証の ON/OFF
- `tls_ca_bundle`: 社内CAなどの証明書バンドル

### `runtime`

- `auto_approve`: `true` なら自動承認
- `interactive_approval`: `false` にすると対話承認しない
- `max_resume`: 承認リジューム上限

## 📦 workspace に保存されるもの

- workflow: `workflow/<workflow_id>/workflow.xml`
- step plan: `plan/<workflow_id>/<step_plan_id>/step_plan_v{n}.json`
- run state: `.orchestra_state/runs/<run_id>.json`
- audit log: `.orchestra_state/audit/events.ndjson`
- snapshots: `.orchestra_snapshots/`

## 🛠️ ローカル実行 (Dockerなし)

MCP サーバー起動:

```powershell
uv run --extra mcp-server orchestra-agent-mcp --config .\orchestra-agent.toml --workspace .\workspace --server files
uv run --extra mcp-server orchestra-agent-mcp --config .\orchestra-agent.toml --workspace .\workspace --server excel
```

CLI 実行:

```powershell
uv run orchestra-agent --config .\orchestra-agent.toml --workspace .\workspace `
  "input/sales.xlsx の C 列を集計して output/summary.xlsx に保存" `
  --mcp-endpoint http://127.0.0.1:8010/mcp `
  --mcp-endpoint http://127.0.0.1:8020/mcp
```

API 起動:

```powershell
uv run orchestra-agent-api --config .\orchestra-agent.toml --workspace .\workspace `
  --mcp-endpoint http://127.0.0.1:8010/mcp `
  --mcp-endpoint http://127.0.0.1:8020/mcp
```

## 👩‍💻 開発

テスト:

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

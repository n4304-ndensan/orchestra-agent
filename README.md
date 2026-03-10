# orchestra-agent

orchestra-agent は、自然言語の作業指示をワークフローに変換し、MCP ツール経由で実行するオーケストレーション基盤です。
Excel は一機能であり、ファイル操作やレビューを含む汎用的な手順自動化を対象にしています。

- 資料: [Current Status and Flow](docs/current-status.md)

## これは何か

orchestra-agent は次を提供します。

- 目的文から StepPlan (DAG) を生成
- MCP ツール (`files`, `excel`, 追加サーバー) の統合実行
- 承認ゲート、スナップショット、監査ログ
- 失敗時の復旧とリプラン
- CLI / API の 2 つの操作面

## 推奨運用: Docker で `workspace` を共有

このリポジトリの `docker-compose.yml` は、すべてのコンテナでホストの `./workspace` を `/workspace/workspace` にマウントします。
作業対象のファイルはすべて `workspace` 配下に置いてください。

例:

- 入力ファイル: `workspace/input/sales.xlsx`
- 出力ファイル: `workspace/output/summary.xlsx`

重要:

- CLI / API で指定するファイルパスは、workspace ルートからの相対パスで書きます
- 例: `input/sales.xlsx`, `output/summary.xlsx`
- `workspace/input/sales.xlsx` のように `workspace/` を付けると二重解決になるため避けてください

## クイックスタート (Docker)

### 1. 事前準備

`.env` を作成して必要な API キーだけ設定します。

```powershell
Copy-Item .env.example .env
```

必要に応じて設定:

- `OPENAI_API_KEY`
- `GEMINI_API_KEY`

作業フォルダを作成します。

```powershell
New-Item -ItemType Directory -Force workspace | Out-Null
New-Item -ItemType Directory -Force workspace/input, workspace/output | Out-Null
```

### 2. 起動

```powershell
docker compose up --build -d
```

ヘルスチェック:

- Files MCP: `http://127.0.0.1:8010/health`
- Excel MCP: `http://127.0.0.1:8020/health`
- Control Plane API: `http://127.0.0.1:9000/health`
- ツール一覧: `http://127.0.0.1:9000/tools`

### 3. CLI で 1 回実行

```powershell
docker compose run --rm orchestra-cli "input/sales.xlsx の C 列を集計して output/summary.xlsx に保存"
```

実行結果や生成ファイルはホスト側 `workspace` に反映されます。

### 4. API で実行 (任意)

Workflow 作成:

```powershell
curl -X POST http://127.0.0.1:9000/workflows `
  -H "Content-Type: application/json" `
  -d '{"name":"Excel summary","objective":"input/sales.xlsx の C 列を集計して output/summary.xlsx に保存","reference_files":["specs/requirements.pdf"]}'
```

Plan 作成:

```powershell
curl -X POST http://127.0.0.1:9000/workflows/wf-sales-summary/plans -H "Content-Type: application/json" -d "{}"
```

Run 開始と承認:

```powershell
curl -X POST http://127.0.0.1:9000/runs `
  -H "Content-Type: application/json" `
  -d '{"workflow_id":"wf-sales-summary","step_plan_id":"sp-...","run_id":"run-1","approved":false}'

curl -X POST http://127.0.0.1:9000/runs/run-1/approval `
  -H "Content-Type: application/json" `
  -d '{"approve":true}'
```

## `workspace` に保存されるデータ

`orchestra-agent.toml` の既定では、次が workspace 配下に保存されます。

- workflow: `workflow/<workflow_id>/workflow.xml`
- step plan: `plan/<workflow_id>/<step_plan_id>/step_plan_v{n}.json`
- run state: `.orchestra_state/runs/<run_id>.json`
- audit log: `.orchestra_state/audit/events.ndjson`
- snapshots: `.orchestra_snapshots/`

## ローカル実行 (Docker を使わない場合)

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

## LLM プロバイダ

- `llm.provider = none`: 決定論ベースの安全寄り動作
- `llm.provider = openai` または `google`: ライブ LLM を使った計画・実行

例 (OpenAI):

```powershell
$env:OPENAI_API_KEY="your_api_key"
uv run orchestra-agent --config .\orchestra-agent.toml --workspace .\workspace `
  "input/sales.xlsx の C 列を集計して output/summary.xlsx に保存" `
  --llm-provider openai --llm-openai-model gpt-4.1-mini
```

## 開発

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

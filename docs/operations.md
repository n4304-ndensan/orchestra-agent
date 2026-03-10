# Operations Guide

`orchestra-agent` を「試す」ではなく「運用する」前提で見るための最小ガイドです。

## Runtime Surfaces

- `Control Plane API`
  - workflow / plan / run / approval / audit の制御面
- `files MCP`
  - workspace 配下の deterministic file operations
- `excel MCP`
  - `.xlsx` の deterministic read / write / extract / save

## Health Endpoints

- `GET /health`
  - プロセスが起動しているかを確認する liveness
- `GET /ready`
  - MCP tool catalog が取得できるかを確認する readiness
- `GET /system`
  - version、workspace、storage root、tool catalog を返す運用向け自己記述 API
- `GET /tools`
  - tool catalog のみを返す軽量 API

運用順序:

1. `/health`
2. `/ready`
3. `/system`

## Environment Variables

`.env.example` に最低限の運用変数を入れています。

- `OPENAI_API_KEY`
- `GEMINI_API_KEY`
- `ORCHESTRA_PUBLISH_HOST`
- `ORCHESTRA_API_PORT`
- `ORCHESTRA_MCP_FILES_PORT`
- `ORCHESTRA_MCP_EXCEL_PORT`
- `ORCHESTRA_MCP_LOG_LEVEL`

初期値は localhost bind です。外部公開したい場合だけ `ORCHESTRA_PUBLISH_HOST` を変更してください。

注意:

- 標準の `orchestra-agent.toml` は `llm.provider = "google"` です
- OpenAI 運用に切り替える場合は config 側も合わせて変更してください

## Storage Layout

- `workspace/workflow/`
  - workflow 本体と履歴
- `workspace/plan/`
  - step plan 本体と履歴
- `workspace/.orchestra_state/runs/`
  - run 単位の現在状態
- `workspace/.orchestra_state/audit/events.ndjson`
  - 監査イベントの追記ログ
- `workspace/.orchestra_snapshots/`
  - failure restore 用スナップショット

## Operational Checks

日常監視で見る項目:

1. `/ready` が `200` を返す
2. `/system` の `tools` が期待 tool を含む
3. `events.ndjson` に `execution_failure` が増えていない
4. `runs/<run_id>.json` が `approval_status = PENDING` で停滞していない

## Incident Handling

run が止まったときの確認順序:

1. `workspace/.orchestra_state/runs/<run_id>.json`
2. `workspace/.orchestra_state/audit/events.ndjson`
3. `workspace/plan/<workflow_id>/<step_plan_id>/step_plan_latest.json`
4. `workspace/workflow/<workflow_id>/workflow.xml`

feedback 修正が必要なとき:

1. `/runs/{run_id}/approval` に `{"feedback":"..."}` を送る
2. 生成された新しい `workflow_v{n}.xml` / `step_plan_v{n}.json` を確認する
3. 再度 approve する

## Security Posture

- file / excel 操作は workspace に閉じる
- approval gate が plan / high-risk step の前後に入る
- LLM への依頼内容と MCP tool call は audit に残る
- 公開ポートはデフォルトで localhost bind

## Release Checklist

1. `pytest -q tests`
2. `ruff check src tests`
3. `/ready` が live MCP で `200`
4. `/system` の tool catalog が想定通り
5. `workspace` への入出力と audit の保存先を確認

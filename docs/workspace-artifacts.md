# Workspace Artifacts Guide

`workspace` 配下にファイルが一気に増えるのは正常です。  
このページは「どのフォルダが何を表すか」を最短で確認するためのガイドです。

## 全体像

- `workspace/workflow/<workflow_id>/`
  - workflow 本体とその履歴
- `workspace/plan/<workflow_id>/<step_plan_id>/`
  - 実行計画 (StepPlan) の履歴
- `workspace/.orchestra_state/runs/`
  - run ごとの現在状態
- `workspace/.orchestra_state/audit/events.ndjson`
  - 監査イベントの時系列ログ
- `workspace/.orchestra_snapshots/`
  - 復旧用スナップショット

## workflow フォルダ

例: `workspace/workflow/wf-57f6e38406/`

- `workflow.xml`
  - 最新の workflow 定義
- `versions/workflow_v1.xml`, `workflow_v2.xml`, ...
  - バージョン履歴
- `feedback/feedback_v2.txt`, ...
  - リプラン時に入れた feedback 履歴

見る順序:

1. `workflow.xml` (今の正)
2. `versions/` (差分履歴)
3. `feedback/` (なぜ変わったか)

## plan フォルダ

例: `workspace/plan/wf-57f6e38406/sp-7e0d405ef9/`

- `step_plan_latest.json`
  - 最新 StepPlan
- `step_plan_v1.json`, `step_plan_v2.json`, ...
  - リプラン含む計画履歴

見る順序:

1. `step_plan_latest.json` (今の実行対象)
2. `step_plan_v*.json` (変更履歴)

## なぜファイルが増えるのか

- `approve` で進めるだけなら、基本は実行ログだけ増えます。
- `feedback` を送ると workflow と step plan を再生成するため、`workflow_v{n}.xml` と `step_plan_v{n}.json` が増えます。
- step 失敗時に自動修復が走った場合も同様に version ファイルが増えます。

目安:

1. `workflow_vN.xml` が増えた: 要件や手順が修正された
2. `step_plan_vN.json` が増えた: 実行ステップが再計画された
3. `feedback/feedback_vN.txt` が増えた: 人手で修正指示を出した

## state と audit

- `workspace/.orchestra_state/runs/<run_id>.json`
  - その run の現在状態 (`approval_status`, `last_error`, `pending_approval` など)
- `workspace/.orchestra_state/audit/events.ndjson`
  - すべての run のイベントログ

run 単位で追う場合は、`events.ndjson` から該当 `run_id` を grep すると早いです。

## どのファイルを見ればよいか (困ったとき)

- Step内容が変だと感じる:
  - `step_plan_latest.json`
- なぜその StepPlan になったか知りたい:
  - `workflow.xml` + `workflow/feedback/*`
- エラー原因を知りたい:
  - `runs/<run_id>.json` の `last_error`
  - `events.ndjson` の `execution_failure`
- 承認前に step を直したい:
  - 承認入力で `feedback` を使う
  - 修正後は `step_plan_latest.json` を確認してから再度 approve

## cleanup 方針

- 調査が終わった run は `workspace/workflow` と `workspace/plan` の対象 ID を整理してよい
- 監査が必要なら `events.ndjson` は保持
- 再現検証に必要なら `workflow_v*.xml` / `step_plan_v*.json` は保持

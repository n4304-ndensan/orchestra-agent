# LLM Step Runtime Protocol

`orchestra-agent` の `orchestra.llm_execute` / `orchestra.ai_review` は、step ごとに 1 回の大きな JSON を返すのではなく、action ベースの往復プロトコルで動きます。

## Goals

- step plan には抽象 step だけを書き、具体 MCP tool 選択は runtime に遅延させる
- MCP server が増えても action schema を変えずに拡張できる
- 各 step の最終成果を `finish.result` に正規化し、次 step に安全に渡す

## Action Contract

基本形では、LLM は各 turn で次のいずれか 1 つを返します。

- `call_mcp_tool`
  - `tool_ref`
  - `input`
- `request_file_attachments`
  - `paths`
  - `reason`
- `write_file`
  - `path`
  - `content`
- `finish`
  - `result`

`finish.result` は必須です。

- この値が step の正式な出力になります
- 次の step では `step_results` として参照されます
- runtime 内では dataclass ベースの parser により検証されます

## Compact Batch Mode

Protocol v2 では、待ち時間を減らすために短い action 列を 1 応答でまとめられます。

```json
{
  "actions": [
    {"type": "call_mcp_tool", "tool_ref": "excel.create_file", "input": {"file": "output.xlsx"}},
    {"type": "call_mcp_tool", "tool_ref": "excel.save_file", "input": {"file": "output.xlsx", "output": "output.xlsx"}},
    {"type": "finish", "result": {"output_file": "output.xlsx"}}
  ]
}
```

制約は次の通りです。

- `request_file_attachments` は単独 turn でのみ使う
- `finish` は batch 内で最後に 1 回だけ使う
- 後続 action が中間結果に依存しない場合だけ batch を使う
- batch は短く保ち、通常は 2 から 4 action 程度にする

## Extensibility

内部実装では action ごとに専用 dataclass を持ちます。

- `CallMcpToolAction`
- `RequestFileAttachmentsAction`
- `WriteFileAction`
- `FinishAction`

各 action は `extensions` を保持できます。

- 現在未使用の追加フィールドを捨てずに保持できる
- 将来 `retry_hint`、`confidence`、`handoff` などを追加しやすい
- `finish` の必須 contract を壊さずに拡張できる

## MCP Tool Catalog

runtime が LLM に渡す `available_mcp_tools` は、`name` と `description` だけでなく `server` などのメタデータを含められます。

- 複数 MCP server を束ねても tool catalog の形を変えずに扱える
- planner は server metadata を runtime hint として使える
- routing 自体は runtime が `tool_ref` から解決する

## Operational Rule

新しい MCP server を追加するときの前提は次の 2 つです。

1. `[[mcp.servers]]` に endpoint を追加する
2. tool 名は全体で一意にする

tool 名が重複した場合、runtime は duplicate registration として reject します。

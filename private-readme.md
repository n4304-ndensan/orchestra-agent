# Private Playwright ChatGPT

`chatgpt_playwright` provider を使って、private 用の Custom GPT を `orchestra-agent` に接続する手順です。

このブランチの想定は `LLM は host の Chrome/ChatGPT`、`MCP files/excel も host` です。`docker compose run orchestra-cli ...` は使わず、host 側で `uv run orchestra-agent ...` を実行してください。

## 前提

- Windows で Google Chrome が入っている
- ChatGPT にログインできるアカウントがある
- `uv sync --extra mcp-server --extra chatgpt-playwright` 済み

## 設定

`orchestra-agent.toml` は private 用に次を既定化しています。

- `llm.provider = "chatgpt_playwright"`
- `llm.provider_modules = ["orchestra_agent.adapters.llm.chatgpt_playwright_provider"]`
- `llm.planner_mode = "full"`
- `llm.chatgpt_url = "https://chatgpt.com/g/g-69919f16473081918e2c40de0c8be30f-shikiho-image-json-extractor"`
- `llm.chatgpt_profile_dir = "./.chatgpt-profile"`
- `mcp.servers[].endpoint = http://127.0.0.1:8010/mcp / http://127.0.0.1:8020/mcp`

`chatgpt_playwright` 自体は core runtime に hardcode していません。public 側は `llm.provider_modules` で external provider module を読むだけで、この private branch では `orchestra_agent.adapters.llm.chatgpt_playwright_provider` を差し込んでいます。private repository 側で module path を差し替えれば、別ブランチの LLM 実装変更と干渉しにくくなります。

`chatgpt_profile_dir` には ChatGPT の login session が保存されます。`.gitignore` 済みです。

Chrome の場所が違う場合だけ `llm.chatgpt_chrome_path` を直してください。

## 起動

まず host 上で MCP を 2 本起動します。PowerShell を 2 つ開いてそれぞれ実行してください。

```powershell
uv run orchestra-agent-mcp --config orchestra-agent.toml --workspace ./workspace --server files
```

```powershell
uv run orchestra-agent-mcp --config orchestra-agent.toml --workspace ./workspace --server excel
```

必要なら control plane も host 上で起動できます。

```powershell
uv run orchestra-agent-api --config orchestra-agent.toml --workspace ./workspace
```

Playwright ChatGPT は host 側 Chrome を使うので、CLI も host で実行します。

```powershell
uv run orchestra-agent run `
  --config orchestra-agent.toml `
  --workspace ./workspace `
  "output/HelloWorld.xlsx を作成し、Sheet1 の A1 に HelloWorld と書き込んで保存して"
```

`mcp.servers` に localhost endpoint を入れてあるので、通常は `--mcp-endpoint` の明示は不要です。

初回は Chrome が開き、ChatGPT 側の login や Enter 入力待ちが入ります。以降は `./.chatgpt-profile` が再利用されます。

依存をまだ入れていない場合:

```powershell
uv sync --extra mcp-server --extra chatgpt-playwright
```

## 画像やファイルを渡す

`--reference-file` を使うと、file attachment が ChatGPT 側へ渡されます。

```powershell
uv run orchestra-agent run `
  --config orchestra-agent.toml `
  --workspace ./workspace `
  --reference-file input/sample.png `
  "この画像を解析してJSON形式で出力してください。"
```

内部では `LlmGenerateRequest.messages` を 1 本の prompt にまとめ、最新メッセージの attachment を `ChatGPTClient.chat(..., file_paths=[...])` へ渡します。

## 使い分け

- `chatgpt_playwright`
  - private Custom GPT をそのまま使いたい時
- `google` / `openai`
  - headless API 実行や Docker 内完結を優先したい時

`chatgpt_playwright` は browser login が前提なので、CI や server-side batch には向きません。

## よくある詰まり方

- `Google Gemini API key is required`
  - `provider` が `google` に戻っているので `chatgpt_playwright` を確認
- `docker compose run orchestra-cli ...` で動かない
  - container から host Chrome は使えないので host で `uv run orchestra-agent ...` を使う
- `MCP endpoint` 接続エラー
  - `orchestra-agent-mcp --server files` と `--server excel` が起動しているか確認する

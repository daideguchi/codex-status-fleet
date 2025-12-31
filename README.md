# Codex Status Fleet

複数の **Codex（ChatGPT サブスク）アカウント** と **Claude（Anthropic API キー）** のレートリミットを、ローカルの Collector に集約して一覧表示するための最小構成です。

- Codex: `codex app-server` の JSON-RPC（`account/rateLimits/read`）
- Claude: Anthropic API への最小リクエストで rate limit ヘッダを取得（キーはローカルに保存）

端末 UI（`/status`）のスクレイピングではなく、**構造化された rate limit 情報**（`usedPercent` / `resetsAt` / `windowDurationMins` など）をそのまま保存します。

メモ:

- `usedPercent` は「使用率」で、Codex の UI が出す「xx% left」は `100 - usedPercent` です
- `windowDurationMins=300` が 5h、`windowDurationMins=10080` が weekly です（`parsed.normalized.windows["5h"]` / `["weekly"]`）
- Claude（Anthropic API）は `parsed.normalized.windows["requests"]` / `["tokens"]` に `limit` / `remaining` / `resetsAtIsoUtc` を入れます

## できること

- アカウントごとに認証情報 (`~/.codex`) を分離して保持
- エージェントが `codex app-server` から `account/rateLimits/read` を取得
- 取得結果を Collector に POST → SQLite に保存
- Anthropic（Claude API）も同じ一覧で確認（`anthropic-ratelimit-*` ヘッダ）
- アカウントレジストリ（`accounts.json` → Collector に一括登録）で、未取得でも一覧に表示
- 最新状態の取得 API
  - `GET /healthz`
  - `GET /latest`
  - `GET /registry`
  - `GET /latest/{account_label}`
  - `GET /events/{account_label}?limit=50`

## 前提

- macOS + Docker Desktop（または Linux）
- ホスト側に `codex` が入っていること（ログイン用）
- Claude を使う場合: Anthropic API キー（`sk-ant-...`）

## セットアップ（ホストでログイン → コンテナで監視）

1) 設定ファイルを作成

```bash
cd codex-status-fleet
cp accounts.example.json accounts.json
```

`manual_refresh` を使うと、**常駐エージェントのポーリングを止めて**必要なときだけ手動で更新できます（おすすめ）。

`accounts.json` の各アカウント項目で、必要なら以下を使えます:

- `enabled`: `false` で compose 生成対象から除外
- `expected_email`: 認証が想定アカウントかチェック（Collector の `parsed.normalized.expected_email_match` で確認）

大量にある場合:

- `/status` の貼り付けメモから `accounts.json` の雛形を生成できます  
  - `python3 scripts/import_status_memo.py --in memo.txt --out accounts.json`
  - 「解約済み」表記を無視して全部 `enabled=true` にしたい場合: `--ignore-canceled`

2) アカウントごとにログイン（認証情報を `accounts/<label>/.codex` に分離）

```bash
./scripts/init_account.sh acc1
./scripts/init_account.sh acc2
```

注意: ふつうに `codex login` を実行すると `~/.codex` に保存されます。Fleet が参照するのは **`accounts/<label>/.codex/`** なので、上の `init_account.sh` を使う（または下の capture を使う）必要があります。

すでに `~/.codex` にログイン済みのアカウントがある場合（今ログインしたアカウントを保存したい）:

```bash
python3 scripts/capture_current_login.py --expected-email you@example.com --expected-plan-type plus --config accounts.json
```

※ `~/.codex/auth.json` をコピーするだけなので、直前にログインしたアカウントのメールを指定してください。

3) アカウント定義から compose を生成

```bash
python3 scripts/generate_compose.py --config accounts.json --out docker-compose.accounts.yml
```

4) 起動

```bash
docker compose -f docker-compose.yml -f docker-compose.accounts.yml up -d --build
```

ショートカット:

```bash
./scripts/up.sh
```

`./scripts/up.sh` は以下も自動で行います:

- `accounts.json` から `docker-compose.accounts.yml` 生成
- `accounts.json` のアカウント一覧を Collector の `/registry` に一括登録（UI の一覧に全件出すため）

5) 動作確認

```bash
curl -s http://localhost:8080/healthz
curl -s http://localhost:8080/latest | python3 -m json.tool
```

ブラウザ UI:

- `http://localhost:8080/`（一覧）

注意:

- 一覧には `/registry` に登録されたアカウントが、まだ未取得でも `pending` として出ます
- ログイン情報（セッション/トークン）は `accounts/<label>/.codex/` に保存され、コンテナ再起動後も保持されます

## 手動更新モード（ポーリングなし）

`accounts.json` に `manual_refresh: true` を入れると、`docker-compose.accounts.yml` に agent サービスを出さず、Collector だけ起動します。

- 起動: `./scripts/up.sh`
- UI から更新: `http://localhost:8080/` を開いて **Update now**（またはブラウザ更新）  
  - `/refresh` が走って最新値を取得し、表も自動で更新されます
- API から更新: `curl -X POST http://localhost:8080/refresh`
- どうしても CLI で更新したい場合（ホスト側実行）:
  - 全件: `python3 scripts/refresh_all.py --config accounts.json`
  - 1件: `python3 scripts/refresh_all.py --config accounts.json --label acc_xxx`

## アカウント追加

1) UI で追加（おすすめ）

- `http://localhost:8080/` → **Add accounts**  
  - メール（1行1件）または `/status` の貼り付けを入れると email を自動抽出して `accounts.json` に追記します
- `http://localhost:8080/` → **Add Claude keys**  
  - `sk-ant-...` を貼り付けると `accounts.json` に `provider: "anthropic"` を追加し、キーを `accounts/<label>/.secrets/anthropic_api_key.txt` に保存します（ログイン不要）
  - 取得時は Anthropic API に最小リクエスト（`max_tokens=1`）を送ってヘッダを読むため、少量ですがリクエスト/トークンを消費します

（CLI で追加したい場合）

```bash
python3 scripts/add_accounts.py --config accounts.json --in emails.txt --plan plus
```

2) まとめてログイン（未ログインだけ / Codex のみ）

```bash
./scripts/login_all.sh accounts.json --device-auth
```

3) UI で **Update now**（またはブラウザ更新）

## 便利コマンド

- ログイン済みか確認: `./scripts/probe_account.sh acc1`
- ログイン済み一覧: `python3 scripts/login_status.py --config accounts.json`
- メール一覧を追加: `python3 scripts/add_accounts.py --config accounts.json --in emails.txt --plan plus`
- レジストリ一括登録のみ: `python3 scripts/push_registry.py --config accounts.json`
- まとめてログイン（未ログインのみ）: `./scripts/login_all.sh accounts.json --device-auth`
- ログ確認: `docker compose -f docker-compose.yml -f docker-compose.accounts.yml logs -f collector`
- 特定アカウント: `docker compose -f docker-compose.yml -f docker-compose.accounts.yml logs -f agent_acc1`
- 停止: `docker compose -f docker-compose.yml -f docker-compose.accounts.yml down`

ショートカット:

- `./scripts/down.sh`

## Collector を別ホストに置く場合

- `accounts.json` の `collector_url` を Collector の URL にする
- `accounts.json` の `collector_in_compose` を `false` にする
- `./scripts/up.sh`（Collector なしで agent だけ起動 + `/registry` に一括登録）

## 次に欲しい情報（実装を固めるため）

- `primary` / `secondary` が何の窓か（例: 5h / weekly）を運用側でどう扱いたいか
- アカウント数・命名規則・取得頻度（秒/分）
- Collector を置く場所（ローカル / VPS / LAN 内）と到達性（HTTP/HTTPS）
- 収集したい「確定項目」（例: usedPercent / resetsAt / planType / credits など）

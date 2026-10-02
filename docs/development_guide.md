# Development Guide

## セットアップ

```bash
cp .env.example .env
make setup && make up && make migrate && make seed
make run     # http://localhost:8000/docs
make worker  # ステージ実行のワーカー（別のターミナルで起動する）
```

ステージ実行の API は受け付けると 202 を返し、ワーカーが実行します。ワーカーなしで動かすときは `.env` に `EXECUTION_MODE=sync` を設定すると、応答の前に実行を終えます（テストも sync で動かす）。

Docker が使えない環境（Cloud Session など）では、ローカルの PostgreSQL 16 を使います。

```bash
sudo service postgresql start
sudo -u postgres psql -c "CREATE ROLE abe LOGIN PASSWORD 'abe' CREATEDB;"
sudo -u postgres createdb -O abe ai_business_explorer
sudo -u postgres createdb -O abe ai_business_explorer_test
```

## 日常のコマンド

| コマンド | 内容 |
|---|---|
| `make format` | Ruff による整形と自動修正 |
| `make lint` | Ruff による lint と整形チェック |
| `make typecheck` | mypy（strict） |
| `make test` | pytest（`TEST_DATABASE_URL` の DB を毎回作り直す） |
| `make check` | 上記すべて + `alembic check` |

## 実際の LLM（Claude API）の接続確認

通常のテストと CI は実際の LLM を呼びません（Fake LLM と、SDK を差し替えた Fake を使う。`APP_ENV=test` では実際のプロバイダーを使わない）。API キーがなくてもすべてのテストが通ります。

接続確認だけは、手動で起動するワークフローで行います。

1. GitHub のリポジトリの Settings → Secrets and variables → Actions で、Repository Secret `ANTHROPIC_API_KEY` を登録する（キーはコード・`.env.example`・Issue などに書かない）。
2. Actions → 「LLM smoke test (manual)」→ Run workflow で、`confirm` に `run` と入力して起動する。
3. Claude API が2回だけ呼ばれる（通常の呼び出しと、最小の構造化出力 `{"ok": true}`）。それぞれのモデル・トークン数・費用・リクエストIDと、構造化出力の確認結果（`structured_ok`）、合計の費用（上限 0.05 USD）が表示される。キーは表示されない。

ローカルで試す場合は `LLM_SMOKE_CONFIRM=yes LLM_API_KEY=... uv run python -m ai_business_explorer.llm_smoke`（`APP_ENV=test` では動かない）。

## Prompt の版の切り替え（既存の DB）

seed は AI社員がないときだけ作るので、Prompt の新しい版（例：idea_generator の v2、market_researcher の v4）を seed に入れても、既存の DB の AI社員は書き換わりません。既存の DB で切り替えるときは、admin が API で更新します（migration は使わない）。

```bash
curl -X PATCH http://localhost:8000/api/v1/ai-employees/<market_researcher の id> \
  -H "X-Actor-Id: <admin の actor id>" -H "Content-Type: application/json" \
  -d '{"prompt_version": "v4"}'
```

Prompt の版で、出力契約（出力モデルと分析の `schema_version`）が決まります（第2回仕様 17章）。対応は各 AI社員の実装の `output_contracts` にあり、`Agent.contract_for` で引きます。

| 実装 | Prompt の版 | 出力契約 | schema_version |
|---|---|---|---|
| idea_generator | v1 | v1 | idea_generation.v1 |
| idea_generator | v2 | v2（日本語・ideas の既定5件・claims 10件まで・出典のない数値は推定と明示） | idea_generation.v2 |
| market_researcher | v1・v2・v3 | v1 | market_research.v1 |
| market_researcher | v4 | v2（上に加えて、一般化は inference・relation は context） | market_research.v2 |

- 新しい seed は idea_generator v2・market_researcher v4 で作ります。v1 の Prompt・出力モデルは変えていないので、切り替えなければ既存の AI社員の挙動は変わりません。
- Prompt のファイル（`prompts/<key>/<version>.md`）がない版、または出力契約のない版は 422 で拒否されます。
- AI社員の版（`version`）が1つ上がり、変更前後が監査ログに残ります。過去の実行は、実行ごとに記録した `prompt_version` と `prompt_hash` で追跡できます。
- LLM に送る出力スキーマは実行のたびにコードから作るので（`output_schema_for`）、AI社員に保存されている `output_format` が古くても、実行には影響しません。`output_format` は作成時の記録で、自動では作り直されません。

## テスト構成

| ディレクトリ | 内容 |
|---|---|
| `tests/unit/` | ステージ定義、Fake LLM、Tool のポリシー、Prompt、AI社員、AI社員から DB への到達禁止（import 検査） |
| `tests/integration/` | DB 制約（人間限定、ai_generated の拒否、ステージ範囲、AI 生成 Idea の出自、ロールは人間のみ、担当の制約、組織の一致） |
| `tests/api/` | API の一連の流れ（AI社員の CRUD、Idea、実行の成功と失敗、Evidence、Analysis、Review、Decision、再実行、差し戻し、監査ログ）、非同期実行（受付・ワーカー・取り消し・タイムアウト・heartbeat・primary / secondary）、データ分類（引き継ぎ・送信上限・下げる操作）、費用と予算・LLM と Tool のログ（記録・上限・本文の保存・保存期間）、Evidence 候補（収集のみ・承認・却下・一括承認・重複と更新版・AI生成の補助情報の分離・来歴。記録した応答を返す Fake Tool を使う）、Web 取得 Tool（組織での有効化・ドメインの許可リスト・SSRF・上限・秘密情報。名前解決と接続を Fake に差し替え、外部には接続しない）、Claude API のクライアント（変換・エラー・再試行なし・キーを出さない・分類・予算・費用の記録。SDK を Fake に差し替える）、ロールごとの操作の可否、組織による分離 |
| `tests/migrations/` | migration による既存データの移行。テストモジュールごとに専用のデータベース（`<TEST_DATABASE_URL のDB名>_<モジュール名>`）を作って使うため、DB ユーザーに CREATE DATABASE の権限が必要 |

LLM は `FakeLLMClient` で、応答は決定的です。失敗のテストでは、`StageRunService(llm_client_factory=...)` に例外を投げる Fake を渡します。

テストの初期化（各テスト後の TRUNCATE）は、凍結した `analysis_evidence_links` のトリガーに止められないよう、そのトランザクションの中だけ `session_replication_role = replica` にしています（テスト用。アプリのコードでは使わない）。

## マイグレーション

```bash
uv run alembic revision --autogenerate -m "..."   # 生成後に必ず目視確認する
uv run alembic upgrade head
uv run alembic check                               # モデルとの差分がないことを確認（CI でも実行）
```

## CI（GitHub Actions）

`.github/workflows/ci.yml` は、PostgreSQL 16 のサービスコンテナ上で次を実行します。

1. Ruff（lint、format）
2. mypy
3. Alembic（upgrade → check → downgrade → upgrade）
4. シード投入を2回（冪等性の確認）
5. pytest

## セキュリティ上の注意

- 秘密情報は `.env` に置きます（Git 管理外）。`LLM_API_KEY` は将来用で、第1回では使いません。
- `LLM_PROVIDER` は `fake` 以外を設定すると、起動時の設定検証で拒否されます。
- AI社員は `AgentContext` だけを受け取り、DB やサービスへアクセスできません。ツールは許可リストと副作用ポリシーで制限しています（第1回の許可は `read_only` のみで、本番用ツールの登録はありません）。
- Prompt の参照はキーとバージョンを正規表現で検証し、パストラバーサルを防いでいます。
- `X-Actor-Id` は認証ではありません。ロールは誤操作の防止にしかならず、ヘッダを偽れば不正に操作できます。外部に公開する環境では使わないでください。

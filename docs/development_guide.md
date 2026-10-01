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

## テスト構成

| ディレクトリ | 内容 |
|---|---|
| `tests/unit/` | ステージ定義、Fake LLM、Tool のポリシー、Prompt、AI社員、AI社員から DB への到達禁止（import 検査） |
| `tests/integration/` | DB 制約（人間限定、ai_generated の拒否、ステージ範囲、AI 生成 Idea の出自、ロールは人間のみ、担当の制約、組織の一致） |
| `tests/api/` | API の一連の流れ（AI社員の CRUD、Idea、実行の成功と失敗、Evidence、Analysis、Review、Decision、再実行、差し戻し、監査ログ）、非同期実行（受付・ワーカー・取り消し・タイムアウト・heartbeat・primary / secondary）、データ分類（引き継ぎ・送信上限・下げる操作）、費用と予算・LLM と Tool のログ（記録・上限・本文の保存・保存期間）、ロールごとの操作の可否、組織による分離 |
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

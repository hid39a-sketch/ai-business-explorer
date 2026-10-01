# AI Business Explorer（AI社員事業探索システム）

将来、複数の「AI社員」がアイデア発掘から総合分析までを担当し、**人間が最終判断する**事業探索システムの基盤です。

第1回（このリポジトリの現状）は、機能を多く作ることではなく、AI社員が増えても壊れにくい基盤を作ることを目的としています。

```
Evidence（根拠） → AI Analysis（AI の分析） → Human Review（人間の確認） → Human Decision（人間の判断）
```

- **Evidence** は外部情報と人間の入力に限ります。AI の生成物は Evidence にできません（DB 制約で拒否します）。
- **AI Analysis** は必ず実行履歴（executions）に紐づき、主張ごとに「根拠あり／推論／推測」を区別します。
- **Human Review / Human Decision** は人間だけが記録できます。AI社員からこれらを起動する経路はありません。
- AI社員・Prompt・LLM・Tool・Analysis・コードのバージョンを実行ごとに記録します。

## 技術スタック

| 用途 | 採用 |
|---|---|
| 言語 | Python 3.12 |
| API | FastAPI（REST `/api/v1`、Swagger `/docs`） |
| DB | PostgreSQL 16、SQLAlchemy 2.0（sync）、Alembic |
| 設定 | pydantic-settings（`.env`） |
| 品質 | pytest、Ruff、mypy（strict）、GitHub Actions |
| 開発環境 | uv、Docker Compose |
| LLM | **FakeLLMClient のみ**（実際の LLM API には接続しない） |

## クイックスタート

```bash
cp .env.example .env
make setup     # uv sync
make up        # PostgreSQL 16（docker compose）
make migrate   # alembic upgrade head
make seed      # 既定組織 / human actor（admin）/ system actor（ロールなし）/ Fake AI社員2体
make run       # http://localhost:8000/docs
make check     # lint + typecheck + test + alembic check
```

Docker を使わない場合は、PostgreSQL 16 を用意して `DATABASE_URL` と `TEST_DATABASE_URL` を設定してください。

## API の使い方（Swagger から）

`/api/v1/stages` と `/api/v1/health` 以外のすべての API に `X-Actor-Id` ヘッダが必要です（閲覧を含む）。操作者は組織に所属する人間で、ロール（admin / reviewer / member / viewer）に応じた操作だけができます（[Architecture](docs/architecture.md) の「組織とロール」）。シードされた人間 actor `00000000-0000-7000-8000-000000000001` は既定組織の admin です。

1. `POST /api/v1/explorations` で探索案件を作成する
2. `POST /api/v1/explorations/{id}/stage-runs` で IdeaGenerator を実行する（Idea 候補が `candidate` で登録される）
3. `POST /api/v1/ideas/{id}/adopt` で人間が採用する（採用しないと後続ステージは実行できない）
4. `POST /api/v1/evidence` で根拠を登録する（`source_type` は `human_input` / `document`）
5. `POST /api/v1/ideas/{id}/stage-runs`（`{"stage_key": "market_research"}`）で MarketResearcher を実行する
6. `POST /api/v1/analyses/{id}/human-reviews` で人間がレビューする
7. `POST /api/v1/ideas/{id}/human-decisions` で人間が最終判断（go / no_go / hold / pivot）を記録する（`adopted` の Idea のみ）

> ⚠️ `X-Actor-Id` は認証ではありません。ヘッダの値をそのまま信頼する簡易方式で、第2回も同じです。ヘッダを偽ればどのロールでも操作できるため、ロールは誤操作の防止にしかならず、不正は防げません。外部に公開する環境では使えません。

## 第1回で実装していないもの

実際の LLM API 接続、Web 検索、外部 API、特許 DB、自律型エージェント、ステージの自動連鎖、評価スコア・ランキング、AI による意思決定、本格的な認証・権限管理、Web UI、課金、本番デプロイ。

詳細は [docs/architecture.md](docs/architecture.md) の「Future Extension」を参照してください。

## ドキュメント

- [Architecture](docs/architecture.md)：構成、設計原則、設計判断、Future Extension
- [Data Model](docs/data_model.md)：テーブル、制約、状態遷移
- [Development Guide](docs/development_guide.md)：開発手順、テスト、CI、マイグレーション
- [AI Employee Specification](docs/ai_employee_spec.md)：AI社員の定義と追加方法

# ローカル（Windows・Docker）の DB を最新にする手順と実施記録

このプロジェクトには、クラウド上の本番DBや本番環境はまだない（README の「第2回で実装していないもの」）。
運用している DB は、各自の PC の Docker（`docker compose` の `db`）の PostgreSQL。
この文書は、その DB を最新の版にして Claude API で動かすまでの手順を、Windows PowerShell の前提でまとめたもの。
[RUNBOOK.md](RUNBOOK.md) は「0008 の DB に 0009 を足す」場合の手順で、版が古い DB はこちらに従う。

## 実施記録（2026-10-02）

| 項目 | 結果 |
|---|---|
| 移行前の DB | migration `0001`（第1回）。探索案件1・Idea 3・Evidence 1・分析2・レビュー1・判断1・監査ログ12。AI社員は Fake の2人（v1） |
| リハーサル | 同じ件数のデータを第1回のコードで一時DBに作り、0001 → 0009 → seed → SQL 01〜03 → 切替 → Fake の実行まで成功してから実施した |
| backup | `pg_dump -Fc`（約 50KB） |
| migration | 0001 → 0009（8段階）成功。既存のデータはすべて残った |
| seed | 単価を追加。既存の AI社員・actor は変わらない |
| SQL 01〜03 | すべて合格（0009、単価5行 OK、AI社員2人 switchable、primary は両ステージ ok） |
| 設定 | `.env` に `LLM_API_KEY`、`ORGANIZATION_MONTHLY_BUDGET=5`、`ORGANIZATION_BUDGET_MODE=hard` |
| 切替 | idea_generator v2・market_researcher v4、llm_config は `anthropic`／`claude-sonnet-5-5`／max_tokens 4000／max_cost_per_execution 0.10 |
| スモーク（実API 2回） | どちらも succeeded。費用の合計 0.027486 USD（IG 0.014814、MR 0.012672）。schema_version は v2、claims は各4件、IG の ideas は5件。request_params は max_tokens（4000）だけ送信 |

## 手順（PowerShell）

以下、コマンドはリポジトリのフォルダ（`docker-compose.yml` があるフォルダ）で実行する。

### 1. 今の版とデータを確かめる（読み取りのみ）

```powershell
docker compose cp docs/production_migration/01_precheck.sql db:/tmp/
docker compose exec db psql -U abe -d ai_business_explorer -P pager=off -f /tmp/01_precheck.sql
```

`alembic_version` が `0008` より古い場合、`llm_calls` などのテーブルがまだないので、01 は途中でエラーになる（ROLLBACK で終わり、DB は変わらない）。データ量は第1回からあるテーブルの件数で確かめる。

### 2. backup

```powershell
docker compose exec db pg_dump -U abe -d ai_business_explorer -Fc -f /tmp/backup.dump
docker compose cp db:/tmp/backup.dump .\backup.dump
```

戻すときは `pg_restore`（`--clean`）でこの dump から戻す。

### 3. コードを最新にして migration と seed

アプリとワーカーを止めてから実行する（新しいコードは最新の版の DB でないと動かない）。

```powershell
git pull
$env:UV_LINK_MODE = "copy"
uv run alembic upgrade head
uv run alembic current        # 0009 (head)
uv run python -m ai_business_explorer.seed
```

### 4. 確かめる（読み取りのみ）

```powershell
foreach ($f in "01_precheck.sql","02_pricing_check.sql","03_employee_consistency.sql") { docker compose cp "docs/production_migration/$f" db:/tmp/ }
docker compose exec db psql -U abe -d ai_business_explorer -P pager=off -f /tmp/01_precheck.sql
docker compose exec db psql -U abe -d ai_business_explorer -P pager=off -f /tmp/02_pricing_check.sql
docker compose exec db psql -U abe -d ai_business_explorer -P pager=off -x -v default_provider=fake -f /tmp/03_employee_consistency.sql
```

判定は [RUNBOOK.md](RUNBOOK.md) の工程 6・10・11 と同じ。`default_provider` は `.env` の `LLM_PROVIDER`（未設定なら fake）。

### 5. 起動・切替・スモーク

- 起動：アプリ（`uv run uvicorn ai_business_explorer.main:app`）とワーカー（`uv run python -m ai_business_explorer.worker`）を別々の PowerShell で動かす。
- 切替と llm_config：[RUNBOOK.md](RUNBOOK.md) の工程 12（llm_config は丸ごと置き換え。provider と model を明示する）。
- スモークと確認：RUNBOOK の工程 13・14。04 の `since` は空白を含まない形（例 `2026-10-02T07:00:00Z`）でも渡せる。

## Windows で起きたことと対処

| 症状 | 原因 | 対処 |
|---|---|---|
| `uv run` が `failed to hardlink file ... (os error 396)` で失敗する | uv のキャッシュ（`%LOCALAPPDATA%\uv\cache`）がクラウド同期の対象などで、ハードリンクが使えない | `$env:UV_LINK_MODE = "copy"`（その PowerShell の間だけ有効）。それでも失敗するなら `$env:UV_CACHE_DIR` を同期の外に変える |
| `docker compose` が `no configuration file provided` | `docker-compose.yml` のないフォルダで実行した | リポジトリのフォルダに `cd` してから実行する |
| 実行が `queued` のまま進まない | ワーカーが動いていない。または、ワーカーの PowerShell をクリックして選択モードになり、一時停止している | ワーカーの画面で Esc を押す。止まっていれば起動し直す（待機中の実行は DB に残り、1回だけ処理される） |
| psql の結果が `(END)` で止まる | ページャー | `q` で戻る。`-P pager=off` を付ける |
| `.env` がない | `.env` を作らずに使っていた（DB の接続先などは既定値と同じ） | 必要な項目だけ書いた `.env` を作る。既定値は `.env.example` を参照 |

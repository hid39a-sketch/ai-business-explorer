# AI Business Explorer 本番移行チェックリスト（PR #13 マージ後）

- 対象：`hid39a-sketch/ai-business-explorer` の PR #13（head `95bdf30`、main から15コミット）
- 状態：**準備のみ**。本番DBには一度も接続していない。各工程は承認後に実施する。
- クラウド上の本番DBはまだない。運用しているのは各自の PC の Docker の DB で、その移行（0001 → 0009）は[LOCAL_WINDOWS.md](LOCAL_WINDOWS.md) の手順で実施した（2026-10-02）。
- 同じフォルダの SQL 01〜04 はすべて `BEGIN TRANSACTION READ ONLY … ROLLBACK` で、データを変えない。
  一時DBで次の流れを再現し、SQL がすべて正しく動くことを確かめた：main のコードで作った DB → 0009 → PR #13 の seed → 切替 → Fake のスモーク。

```sh
# 値はここに書かない。シェルの履歴に残らない方法で設定する
export DATABASE_URL=...   # アプリ・alembic 用（postgresql+psycopg://...）
export PSQL_URL=...       # psql 用（postgresql://...）
P="psql $PSQL_URL -v ON_ERROR_STOP=1"
```

## 事前に決めておくこと（承認事項）

| 項目 | 内容 |
|---|---|
| A. 本番モデル | AI社員ごとの `llm_config`（provider・model・max_tokens・max_cost_per_execution） |
| B. 単価の投入方法 | 全 seed にするか、単価だけにするか（工程 9） |
| C. スモークの実施と費用上限 | 実API 2回。上限は「2 × max_cost_per_execution」 |
| D. 本番運用の開始判断 | 工程 15 |

## チェックリスト

| # | 実行するもの | 目的 | 成功条件 | 止める条件 |
|---|---|---|---|---|
| 1 | GitHub で PR #13 を merge（merge commit） | コードを main に入れる | PR が merged、main の HEAD に 95bdf30 の内容が入っている | merge でコンフリクト／PR の head が 95bdf30 でない |
| 2 | main の CI（`.github/workflows/ci.yml`、push で起動）を確かめる | main 上で lint・mypy・migration の往復・seed 2回・pytest が通ること | `check` が success | failure（工程 3 以降に進まない） |
| 3 | ワーカー（`python -m ai_business_explorer.worker`）を止め、API を止めるか書き込みを止める | 移行中に新しい実行と LLM 呼び出しが起きないようにする | ワーカーのプロセスがない | 止められない |
| 4 | `pg_dump -Fc "$PSQL_URL" -f ave_before_0009_$(date +%Y%m%d%H%M).dump`、続けて `pg_restore -l <dump> > /dev/null` | 戻すためのバックアップ | どちらも終了コード 0 | どちらかが失敗 |
| 5 | `$P -f 01_precheck.sql` | 今の状態を確かめる | 下の「工程 6」の条件を満たす | SQL がエラー |
| 6 | 工程 5 の結果を読む | 0009 を適用してよいか判断する | alembic_version＝`0008`、request_params 列なし（0行）、queued/running の実行が0行。件数を控える | `0008` 以外（`0009` ならすでに適用済みなので、工程 8 に飛ぶか調べる。`0008` より古いなら [LOCAL_WINDOWS.md](LOCAL_WINDOWS.md) の手順で head まで上げる）、列がすでにある、実行中がある |
| 7 | `uv run alembic upgrade 0008:0009 --sql` で SQL を確かめてから、`PGOPTIONS='-c lock_timeout=5s' uv run alembic upgrade 0009`（PR #13 のコードで実行） | `llm_calls.request_params`（JSONB、nullable）を追加する | `Running upgrade 0008 -> 0009` | --sql の結果が ALTER 1文と版の更新以外を含む／lock timeout（時間をおいてやり直す）／その他のエラー |
| 8 | `$P -f 01_precheck.sql`。続けて PR #13 のコードを配備する（ワーカーはまだ止めておく） | 適用を確かめる | alembic_version＝`0009`、列が jsonb で nullable、件数が工程 6 と同じ | どれかが違う |
| 9 | 単価を入れる（下の「単価の投入」の方法 A か B） | 4モデルの単価を登録する | コマンドが成功 | エラー |
| 10 | `$P -f 02_pricing_check.sql` | 単価を確かめる | 5行すべて `OK` | `MISSING` や `DIFFERENT` が1行でもある |
| 11 | `$P -v default_provider=<本番の LLM_PROVIDER。未設定なら fake> -f 03_employee_consistency.sql` | 既存の AI社員と primary の割り当てを確かめる | 切り替える AI社員がすべて `switchable` か `already_target`、`has_pricing`＝t、primary_check がどちらのステージも `ok` | `UNKNOWN_IMPLEMENTATION`・`PROMPT_MISSING`・`NO_CONTRACT`・`STAGE_MISMATCH`・`NO_PRIMARY`・`PRIMARY_NOT_ACTIVE` がある |
| 12 | AI社員を切り替える（下の「切替」の PATCH） | IG v2 と MR v4、本番モデル（A）にする | 200、version が増える、output_format の title が `*OutputV2`、claims.maxItems＝10。03 を再実行して `already_target` | 422（エラーの detail を記録して止める） |
| 13 | ワーカーを起動し、スモーク（実API 2回。承認 C の後） | 本番の経路で IG と MR が動くことを確かめる | stage run がどちらも `succeeded` | failed／402・409／費用が上限を超える |
| 14 | `$P -v since="'<工程 13 の開始時刻>'" -f 04_post_smoke_check.sql`。あわせてログで API キーを検索する | 記録・費用・契約を確かめる | 下の「工程 14 の期待値」をすべて満たす | 1つでも違う／API キーがログにある |
| 15 | API とワーカーを通常どおり動かす（承認 D の後） | 本番運用を始める | 最初の通常の実行が succeeded | 工程 16 の条件に当たる |
| 16 | rollback（下の表） | 問題が出たときに戻す | — | — |

### 工程 7 と 8：0009 と配備の順番（現在のコードで確かめた）

- **PR #13 のコードは 0009 の前には配備できない。** `LLMCall` に `request_params` があるので、llm_calls を読み書きするところで `column llm_calls.request_params does not exist` になる（一時DBで確認）。
- **main（a17e6b2）のコードは 0009 の DB でも動く。** 一時DBで、main のコードを使って Fake のステージ実行が succeeded になった。llm_calls の行は request_params が null になる。0009 は列を足すだけで、古いコードを壊さない。
- したがって順番は「0009 を適用 → PR #13 のコードを配備」。逆は不可。
- PostgreSQL 16 では、既定値なしの nullable 列の追加は表を書き換えない。ACCESS EXCLUSIVE ロックは一瞬なので、lock_timeout を付ける。
- main のコードの alembic は `0009` を知らない（`Can't locate revision identified by '0009'`）。コードを main に戻したら、そのコードで `alembic upgrade` や `make migrate` を実行しないこと。

### 工程 9：単価の投入

単価（USD／100万トークン。`seed.py` の `SEED_LLM_PRICING`）：

| provider | model | 入力 | 出力 |
|---|---|---|---|
| anthropic | claude-opus-5-5（anthropic の既定モデル） | 4 | 20 |
| anthropic | claude-sonnet-5-5 | 2 | 10 |
| anthropic | claude-haiku-4-5 | 1 | 5 |
| anthropic | claude-haiku-4-5-20251001 | 1 | 5 |
| fake | fake-model-v1 | 0 | 0 |

- 単価の行は、（kind, provider, model）の行が1行もないときだけ足す。effective_from は 2026-01-01、per_call は 0、通貨は USD。
- 既存の行は、単価が違っても、effective_from が未来でも変えない。そのため、工程 10 の 02 で `DIFFERENT` か `MISSING` が残る。
- main のコードの seed で作った DB では、opus と fake はすでにあり、sonnet と haiku（2つ）の3行が足りないことを一時DBで確かめた。

**方法 A：全 seed**（`uv run python -m ai_business_explorer.seed`）。単価以外に、次のものがなければ作る：
- 既定の組織（固定 ID）
- 人間の actor（固定 ID）と、その admin のロール
- system actor
- key が `idea_generator`／`market_researcher` の AI社員（llm_config は Fake、Prompt は v2/v4）
- 各ステージの primary の割り当て（primary がいないとき）

既存のものは変えない。2回実行しても増えない（CI と一時DBで確認）。
**本番の組織が既定の組織で、上の AI社員と primary がすでにあるときだけ、副作用がない。** 工程 11 の 03 を先に読んで確かめる（03 は読み取りだけなので、工程 9 の前に実行してよい）。

**方法 B：単価だけ**（全 seed の副作用を避ける）：
```sh
uv run python -c "from ai_business_explorer.seed import _seed_pricing; from ai_business_explorer.config import get_settings; from ai_business_explorer.infrastructure.db.session import build_engine, build_session_factory; s = build_session_factory(build_engine(get_settings().database_url))(); _seed_pricing(s); s.commit(); print('pricing seeded')"
```
seed の単価の部分だけを実行する。足すだけで、既存の行は変えない。

**既存の単価が違う（DIFFERENT）とき：** seed では直らない。単価を変えるなら、effective_from を新しくした行を足す（`find_pricing` は effective_from が now 以前で最も新しい行を使う）。どの単価にするかは、その時点で判断して承認を得る。

### 工程 12：切替の PATCH

```sh
curl -sS -X PATCH "$API/api/v1/ai-employees/<id>" \
  -H "X-Actor-Id: <admin の人間の actor id>" -H "Content-Type: application/json" \
  -d '{"prompt_key": "idea_generator", "prompt_version": "v2",
       "llm_config": {"provider": "anthropic", "model": "<承認したモデル>", "max_tokens": 4000, "max_cost_per_execution": "0.10"}}'
# market_researcher は "prompt_key": "market_researcher", "prompt_version": "v4"
```

- `output_format` は指定しない。PR #13 の 13e52ce から、新しい契約のスキーマに自動で入れ直される（一時DBで確認）。
- **`llm_config` は丸ごと置き換わる**（部分的な更新ではない）。指定しなかった項目は消える（一時DBで確認）。
- **PATCH で `provider` を省略すると、provider のない llm_config が保存され、実行時は環境変数 `LLM_PROVIDER`（未設定なら fake）で決まる**（一時DBで確認。作成時に省略すると `fake` が保存される）。環境変数に左右されないよう、provider と model を必ず明示し、残したい上限もすべて書く。
- provider か model が変わる PATCH では、単価を確かめる（なければ 422）。Prompt だけを変える PATCH では確かめないので、単価は工程 10 で確かめておく。
- secondary はスモークの対象外。stage run の body で `secondary_ai_employee_ids` を指定したときだけ動く。

### 工程 13：スモークの内容と最大費用

- 前提：
  - `LLM_API_KEY` がワーカーの環境にある
  - `APP_ENV` が `test` でない（`test` だと実APIは使えない）
  - `EXECUTION_MODE=async` のとき、ワーカーが動いている
  - Exploration の分類は既定の internal で、送信の上限（既定は internal）に収まる
- 手順（body は Swagger の定義どおり）：
  1. `POST /api/v1/explorations` `{"title": "...", "theme": "..."}`
  2. `POST /api/v1/explorations/{id}/stage-runs` `{}` → IG（primary だけで1回呼ぶ）
  3. `POST /api/v1/ideas/{idea_id}/adopt` `{"reason": "smoke"}`
  4. `POST /api/v1/ideas/{idea_id}/stage-runs` `{"stage_key": "market_research"}` → MR（1回呼ぶ）

  一時DBで、Fake のこの流れが通ることを確かめた。
- 最大の呼び出し回数：2回（SDK の再試行はしない。max_retries=0）。
- 最大の費用：1回の実行ごとに `max_cost_per_execution` まで。呼ぶ前に、残りの費用で払える分まで max_tokens を絞る（入力は文字数で多めに見積もる）。0.10 なら2回で 0.20 USD 以下。
  - 参考（前回の実測、1回）：Haiku 0.004〜0.008、Sonnet 0.019〜0.021 USD。Opus は実測がない。

### 工程 14 の期待値（04）

- llm_calls：
  - status＝succeeded、provider＝anthropic、model が承認したもの
  - prompt_version が v2／v4
  - `pricing_id` があり、cost_amount > 0
  - provider_request_id がある
- request_params：
  - `max_tokens` が `{"sent": true, ...}`
  - `thinking` と `effort` が `{"sent": false}`
  - `temperature` は Haiku だけ `{"sent": true, "value": 0}`、ほかは `{"sent": false}`
- analyses：
  - schema_version が `idea_generation.v2`／`market_research.v2`
  - claims が10件以下
  - IG の ideas（`body.data.ideas`）が1〜20件
- executions：succeeded で、prompt_version が v2／v4
- ログ：API キーの文字列が0件

### 工程 16：rollback の条件と方法

| 条件 | 方法 |
|---|---|
| 工程 7 が失敗 | トランザクションが戻るので何もしない。ワーカーを main のコードのまま再開する |
| 工程 8 の後で、PR #13 のコードに障害 | main のコードに戻す（0009 のままでよい）。main のコードで alembic を実行しない |
| 工程 12 の後のスモークで、出力契約のエラーが続く | PATCH で `prompt_version` を v1（MR は元の版）に戻す。output_format も自動で戻る |
| 費用が上限を超えた／課金の記録が合わない | ワーカーを止め、該当する AI社員を `inactive` にし、llm_calls を調べる |
| API キーがログに出た | ログを隔離し、キーを再発行する |
| DB を 0008 に戻す必要がある | 先にコードを main に戻す。そのあと PR #13 のコードで `alembic downgrade 0008`（request_params は消える）。それでもだめなら工程 4 の dump から戻す |
| 単価の行を消したい | 通常は不要。消すなら、工程 9 で足した行（created_at で特定）だけを消す |

## モデル設定の現状（決めていない）

- 設定する場所：AI社員ごとの `ai_employees.llm_config`（provider・model・max_tokens・max_llm_calls・max_tool_calls・max_cost_per_execution）。provider を省略すると環境変数 `LLM_PROVIDER`（既定は fake）。anthropic で model を省略すると `claude-opus-5-5`。
- seed の AI社員は Fake（fake-model-v1）。本番の既存の AI社員の値は、工程 11 の 03 で分かる。
- Haiku 4.5（`claude-haiku-4-5`／`-20251001`）だけ temperature=0 を送る。thinking と effort はどのモデルでも送らない。
- 4モデルとも、単価の seed がある。実API で確かめたのは Haiku 4.5 と Sonnet 5.5 だけ。Opus 5.5 はまだ。
- 環境変数（コードの既定値）：
  - `LLM_CALL_TIMEOUT_SECONDS`＝120、`STAGE_RUN_TIMEOUT_SECONDS`＝600
  - `EXECUTION_MAX_COST`＝1 USD（llm_config で個別に上書きできる）
  - `ORGANIZATION_MONTHLY_BUDGET`＝100 USD（hard）

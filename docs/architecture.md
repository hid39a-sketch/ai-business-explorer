# Architecture

## 目的と原則

- AI社員を1人から多数へ増やしても壊れにくい基盤にする。
- **Evidence / AI Analysis / Human Review / Human Decision を分離する。** AI の分析を事実として扱わず、AI の判断を最終決定として扱わない。
- 人間が後から検証できるように、すべての実行についてバージョンと入力を記録する。
- 第1回のスコープ外の機能は作らない（「Future Extension」に記録する）。

## レイヤー構成

```
[Swagger / curl / pytest]
        │  REST /api/v1（X-Actor-Id）
        ▼
api/v1/          ルーター、レスポンススキーマ、依存関係（操作者の解決）
        │
application/     ユースケース（サービス）。人間専用操作のガードはここ
        │
├─ domain/       列挙値、ステージ定義、ドメイン例外（フレームワーク非依存）
├─ agents/       AI社員の実装（AgentContext のみに依存。DB・サービスには触れない）
├─ llm/          LLMClient プロトコル、FakeLLMClient、使用量の集計
├─ tools/        Tool プロトコル、レジストリ、許可リストと副作用ポリシー（実ツールなし）
├─ prompts/      Prompt ファイル（<key>/<version>.md）とローダー
└─ infrastructure/db/   SQLAlchemy モデル、Repository、セッション
        ▼
PostgreSQL 16
```

依存の方向は `api → application → (domain, agents, llm, tools, infrastructure)` です。`agents/` から `application/`・`infrastructure/`・`sqlalchemy` を import していないことは、テスト（`tests/unit/test_agents.py`）で検査しています。

## 組織とロール（第2回）

- すべての業務データは組織（`organization_id`）に属します。第2回は既定組織1つで運用し、組織を作る API はありません。
- API は `X-Actor-Id` から操作者を特定し、所属組織とロールを解決します（`api/v1/deps.py` の `get_principal`）。`/stages` と `/health` 以外のすべての API は、この解決を必ず通ります（テストで検査）。
- セッションは操作者の組織に限られ、他組織のデータは「存在しない」として 404 を返します（`repositories.scope_to_organization`）。
- ロールを持てるのは人間だけです。AI・system actor はロールを持てないため、閲覧も含めて API を使えません。
- 認証はありません。ロールは誤操作の防止のためのもので、不正は防げません。

| 操作 | 必要なロール |
|---|---|
| 閲覧 | viewer 以上 |
| 探索案件・Idea の作成と更新、Evidence の登録と撤回、ステージの実行・再実行 | member 以上 |
| 差し戻し、Human Review、Human Decision、Idea の採否 | reviewer 以上 |
| AI社員の登録・更新（設定変更） | admin |
| ステージ担当（primary / secondary）の設定変更 | admin |
| ステージ実行の取り消し | member |

上位のロールは下位のロールの操作もできます（viewer < member < reviewer < admin）。

## AI社員の実行フロー（非同期）

1. 人間が API からステージ実行を起動する（自動で次のステージへは進まない）。
2. 事前検証：Idea が `adopted` か、前のステージに成功した最新の試行があるか、担当 AI社員が `active` で実装があるか、同じ範囲で置き換える試行がまだ終わっていない（`queued` / `running`）ことはないか（あれば 409）。
3. 担当を決める。primary は、人間が指定した `ai_employee_id` → `stage_assignments` の primary → 有効な社員が1人だけならその社員、の順。secondary は人間が `secondary_ai_employee_ids` で選んだ社員で、そのステージに secondary として割り当てられている必要がある。
4. `stage_runs` と `executions`（primary と secondary それぞれ1件）を `queued` で作成してコミットし、**202** を返す。再実行・差し戻しの場合は、対象ステージ以降の最新試行に `superseded_at` を記録する。
5. ワーカー（`make worker`）が最も古い `queued` を1つ取り出し（`FOR UPDATE SKIP LOCKED`）、`running` にして実行する。実行中は別のセッションで `heartbeat_at` を更新する。`EXECUTION_MODE=sync`（テストと Fake LLM 用）では、応答の前に同じ処理で実行する。
6. AI に渡す入力を、実行を始めた時点で決める。Evidence は active のものだけ（superseded・retracted・purged は渡さない）。前段の分析は、成功した最新の試行の **primary** のものだけ。渡した ID と状態は `stage_runs.input_snapshot` に残す。
7. primary を先に、続いて secondary を実行する。`AgentContext`（読み取り専用の入力、LLM、ToolBox、Prompt）を組み立てて AI社員を実行する。
8. 出力を検証する：claim の ID が一意か、参照している Evidence が入力に含まれるか、relation の規則（重複なし、supports と contradicts の同時指定なし、`evidence_based` は supports か contradicts が必須）を守っているか、Idea 候補を出せるのは idea_generation だけか。
9. 成功した場合：`analyses`、`claims`、`claim_evidence_links`、（idea_generation の primary なら）`candidate` の Idea を1トランザクションで保存する。secondary の出力からは Idea を作らない。第1回の `analysis_evidence_links` には書き込まない（凍結済み）。
10. 失敗した場合：部分的な出力をロールバックし、別トランザクションでその execution に `failed` と `error_type` を記録する。
11. stage_run の状態は primary の結果で決まる（primary が成功なら `succeeded`、それ以外は `failed`）。secondary が失敗しても stage_run は失敗にしない。

### 取り消し・タイムアウト・heartbeat

- **取り消し**：人間（member 以上）が `POST /stage-runs/{id}/cancel` で `queued` / `running` の実行を `cancelled` にする。まだ終わっていない execution も `cancelled` になる。ワーカーは LLM・Tool を呼ぶ前と、出力を保存する直前に状態を確認して止まり、取り消し後に返ってきた出力は保存しない（すでに送った LLM の呼び出しは止められない）。`cancelled` は後から上書きしない。
- **タイムアウト**：ステージ実行全体（`STAGE_RUN_TIMEOUT_SECONDS`、既定 600 秒）を超えたら、次の LLM・Tool の呼び出しの前に止めて `failed`（`timeout`）にする。LLM の1回の呼び出しの上限（`LLM_CALL_TIMEOUT_SECONDS`、既定 120 秒）は LLM アダプターに渡す。
- **heartbeat**：`heartbeat_at` が `WORKER_HEARTBEAT_TIMEOUT_SECONDS`（既定 60 秒）より古い `running` の実行は、いずれかのワーカーが `failed`（`unexpected`、"worker heartbeat lost"）にする。
- どの場合も、自動の再実行・次のステージへの連鎖はしない。人間が再実行する。

## 人間専用の操作（AI からの経路なし）

| 操作 | ロール | アプリ層 | DB 層 |
|---|---|---|---|
| Human Review | reviewer 以上 | `require_human` | 複合 FK（actor_id, actor_type）+ `CHECK (reviewer_actor_type = 'human')` |
| Human Decision | reviewer 以上 | `require_human` + 対象 Idea が `adopted` であること | 同上 |
| ステージ実行・再実行 | member 以上 | `require_human` | `stage_runs` に同様の複合 FK + CHECK |
| ステージ実行の取り消し | member 以上 | `require_human` | （監査ログに記録） |
| ステージ担当の設定 | admin | `require_human` | （監査ログに記録） |
| 差し戻し | reviewer 以上 | `require_human` | 同上 |
| Idea の採用・却下 | reviewer 以上 | `require_human` | （監査ログに記録） |
| Idea の更新 | member 以上 | `require_human` | （監査ログに記録） |
| Evidence の登録・撤回 | member 以上 | `require_human` | `source_type` から `ai_generated` を CHECK で排除 |
| ロールの付与 | — | — | `organization_memberships` に複合 FK + `CHECK (actor_type = 'human')` |

ロールの確認は第1回の人間限定のガードと DB 制約の上に重ねたもので、それらを置き換えたり緩めたりはしません。

## バージョン追跡

| 対象 | 記録先 |
|---|---|
| AI社員の定義 | `ai_employees.version`（更新のたびに増える）と `executions.ai_employee_snapshot` |
| Prompt | `executions.prompt_key / prompt_version / prompt_hash`（SHA-256） |
| LLM | `executions.llm_provider / llm_model / usage` |
| Tool | `executions.output.tool_calls`（name, version, side_effect） |
| Analysis | `analyses.schema_version / version_no / supersedes_id`。版の連鎖は「範囲 × ステージ × AI社員」単位 |
| コード | `executions.code_version`（git SHA。取得できなければ `unknown`） |
| 入力 | `stage_runs.input_snapshot`、`executions.input`（使った Evidence と Analysis の ID） |

## 設計判断（第1回で確定したもの）

| 判断 | 内容 |
|---|---|
| AI社員の正本 | DB（`ai_employees`）。コード実装は `implementation_key` で参照し、実装のない社員は実行できない |
| Idea の詳細項目 | 人間だけが更新する。AI が書けるのは候補生成時の title / summary / problem だけ |
| Idea の項目の型 | すべて自由記述テキスト。スコアは持たない |
| 調査ステータス | 保存せず、`current_stage_key` と最新の stage_run から算出する |
| レビュー | `analyses.review_status` は ReviewService だけが更新する。修正は `human_reviews.corrections` に入れ、AI Analysis 本体は不変。主張単位のレビュー（`claim_id`）は `review_status` を変えない |
| 監査ログ | 最小限（作成・更新・状態遷移・レビュー・決定・差し戻し） |
| LLM・ツール呼び出しの明細 | Future Extension（第1回は executions に集約） |
| ステージの進め方 | 1ステージずつ人間が API から実行する。自動連鎖しない |
| Idea の初期状態 | AI 生成も人間作成も `candidate`。adopt / reject は人間だけ |
| Human Decision の条件 | 対象 Idea が `adopted` のときだけ記録できる（`candidate` / `rejected` では 409） |

## Future Extension

第1回では実装していないもの：

- 実際の LLM アダプター（`llm/adapters/`）とトークン使用量の実測
- `llm_calls` / `tool_calls` の明細テーブル
- 実ツール（Web Search、Patent Search、News、Financial Data、Internal DB / Knowledge Base）と、ツール経由の Evidence 自動登録
- 残りの AI社員（CompetitorResearcher、TechnologyResearcher、PatentResearcher、MonetizationAnalyst、RiskAnalyst、BusinessAnalyst）
- AI社員同士の相互検証
- ステージの自動連鎖、DAG・並列ステージ（非同期実行・タイムアウト・取り消しは第2回 PR-4 で実装）
- 本格的な認証（`api/v1/deps.py` の `get_principal` で操作者を特定する部分を差し替える）、レビュー画面
- 評価軸・スコア・ランキング
- Evidence のベクトル検索（pgvector）
- 追記専用テーブルを DB トリガーで UPDATE / DELETE 禁止にする
- CI でのシークレットスキャン・依存関係の自動更新

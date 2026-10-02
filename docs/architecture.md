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
| 予算の設定 | admin（費用の集計・呼び出しのメタデータの閲覧は viewer） |
| LLM ログの本文の閲覧 | admin |
| 探索案件のデータ分類を下げる | admin（上げるのは member） |
| ステージ実行の取り消し | member |

上位のロールは下位のロールの操作もできます（viewer < member < reviewer < admin）。

## AI社員の実行フロー（非同期）

1. 人間が API からステージ実行を起動する（自動で次のステージへは進まない）。
2. 事前検証：Idea が `adopted` か、前のステージに成功した最新の試行があるか、担当 AI社員が `active` で実装があるか、同じ範囲で置き換える試行がまだ終わっていない（`queued` / `running`）ことはないか（あれば 409）。
3. 担当を決める。primary は、人間が指定した `ai_employee_id` → `stage_assignments` の primary → 有効な社員が1人だけならその社員、の順。secondary は人間が `secondary_ai_employee_ids` で選んだ社員で、そのステージに secondary として割り当てられている必要がある。実装・Prompt（key と version の両方）・解決後の（provider, model）がすべて primary と同じ secondary は使えない（V-08。割り当ての作成時と起動時の両方で確かめ、違反は 422）。
4. `stage_runs` と `executions`（primary と secondary それぞれ1件）を `queued` で作成してコミットし、**202** を返す。再実行・差し戻しの場合は、対象ステージ以降の最新試行に `superseded_at` を記録する。
5. ワーカー（`make worker`）が最も古い `queued` を1つ取り出し（`FOR UPDATE SKIP LOCKED`）、`running` にして実行する。実行中は別のセッションで `heartbeat_at` を更新する。`EXECUTION_MODE=sync`（テストと Fake LLM 用）では、応答の前に同じ処理で実行する。
6. AI に渡す入力を、実行を始めた時点で決める。Evidence は active のものだけ（superseded・retracted・purged は渡さない）。前段の分析は、成功した最新の試行の **primary** のものだけで、レビューの状態に関係なく渡す（reject されたものも除外しない）。前段の分析には、その時点のレビューの状態（`review_status`）と、分析全体（claim_id なし）の最新のレビュー1件（decision・comment・corrections）を付ける（V-07）。渡した ID と状態（Evidence の状態、分析のレビューの状態と最新のレビューの ID）は `stage_runs.input_snapshot` に残し、後でレビューが変わっても書き換えない。
7. primary を先に、続いて secondary を実行する。`AgentContext`（読み取り専用の入力、LLM、ToolBox、Prompt）を組み立てて AI社員を実行する。
8. 出力を検証する：claim の ID が一意か、参照している Evidence が入力に含まれるか、relation の規則（relation は必須、重複なし、supports と contradicts の同時指定なし、`evidence_based` は supports か contradicts が必須）を守っているか、Idea 候補を出せるのは idea_generation だけか。構造化出力のスキーマ（下の「実際の LLM」）で形を保証したうえで、スキーマで表せない規則をここで確かめる。
9. 成功した場合：`analyses`、`claims`、`claim_evidence_links`、（idea_generation の primary なら）`candidate` の Idea を1トランザクションで保存する。secondary の出力からは Idea を作らない。第1回の `analysis_evidence_links` には書き込まない（凍結済み）。
10. 失敗した場合：部分的な出力をロールバックし、別トランザクションでその execution に `failed` と `error_type` を記録する。
11. stage_run の状態は primary の結果で決まる（primary が成功なら `succeeded`、それ以外は `failed`）。secondary が失敗しても stage_run は失敗にしない。

### データ分類（第2回仕様 11章）

- 分類は `public` < `internal` < `confidential` < `restricted` の4段階。探索案件と Evidence に付け、既定は `internal`。Idea は探索案件の分類に従う（列を持たない）。
- 分析の分類（`analyses.classification`）は、その実行の入力（探索案件・Evidence・前段の分析）の最も高い分類。算出値で、人間も変更できない。前段の分析を通じて後続ステージに引き継がれる。
- LLM プロバイダーごとの送信上限は設定 `LLM_MAX_CLASSIFICATION`（JSON、例：`{"fake": "internal"}`）で持つ。指定のないプロバイダーは `internal`。契約条件を確認するまで `internal` のままにする（R-03）。`restricted` はどの LLM にも送らない（設定しても拒否する）。
- 起動時：入力の最も高い分類が、primary・secondary の各 AI社員のプロバイダーの上限を超えたら 409 で拒否し、実行記録も作らない。
- 実行時：受付の後に分類が上がった場合に備え、ワーカーは LLM を呼ぶ前にもう一度確認し、超えていれば LLM に送らずにその execution を `failed`（`validation_error`）にする。
- 探索案件の分類を下げる変更は admin のみ（上げるのは member 以上）。Evidence の分類は登録時に決め、後から変えない（訂正は撤回＋新規登録）。撤回した Evidence は入力にならないので、分類の確認にも数えない。
- 運用：機密性の高い資料を登録するときは、Evidence の `classification` を明示する。分類を誤って低く登録した場合は、その Evidence を撤回して正しい分類で登録し直す。

### Evidence 候補と収集のみ（第2回仕様 2章・3章・4章）

- Tool が取得した情報は、まず `evidence_candidates`（候補）に保存する。候補が持つのは原情報（URL・title・取得日時・メタデータ・抜粋（最大 2,000字）・全文）だけ。AI が書いた要約・解釈は `evidence_candidate_ai_notes`（AI生成の補助情報）に分け、元の候補と生成した実行を必ず参照する（B-21）。
- 候補の状態は `pending → accepted / rejected / duplicate`。承認・却下は member 以上の人間だけ（却下は理由必須）。承認すると Evidence が作られ、承認した人間が登録者（`created_by_actor_id`）になる。人間の要約（`summary`）は承認時に受け取り、原情報（`quote`）と分けて保存する。承認は補助情報を読まないので、補助情報が Evidence に移る経路はない。
- 重複判定：同じ探索案件に `source_key` と `snapshot_hash` が同じ active な Evidence があれば `duplicate`（承認不要）。`source_key` だけが同じなら更新版の候補（`updates_evidence_id`）で、承認すると前の版を置き換える（前の版は superseded。撤回ではない）。判定は候補を作るときと承認するときの両方で行う。
- Tool 取得の Evidence は来歴（`acquisition_method = tool`、`candidate_id`、`tool_call_id`、`execution_id`、`retrieved_at`）を必ず持ち、応答の `provenance` に取得方法・Tool 名・検索語・登録した人間をまとめて返す。
- 未承認の候補と補助情報は、AI の入力にも根拠にもならない。
- ステージ実行の `mode`：`analyze`（既定）と `collect_only`。collect_only は Tool で候補を集めるだけで分析・主張・Idea 候補を作らない。LLM を使ってよく、費用は記録する。「最新の試行は1つ」の制約・再実行・後続の入力には数えない。基本の流れは ① collect_only で集める → ② 人間が承認する → ③ analyze で分析する。
- 保存期間（R-20）：Tool の生の出力（`tool_call_outputs`）は 90日、候補の全文は 180日で `make retention` が消す。抜粋・ハッシュ・来歴は残す。
- 組織ごとの自動承認ポリシー、duplicate を pending に戻す操作（E-03）は第2回では作らない。

### 実際の LLM：Anthropic Claude API（第2回仕様 R-03・PR-9）

- プロバイダーは `fake`（Fake LLM。既定・テスト・CI）と `anthropic`（Claude API）の2つ。AI社員の `llm_config.provider` を `anthropic` にした場合だけ Claude API を呼ぶ。モデルの既定は `claude-opus-5-5`（単価 $4 / $20 per 1M tokens を seed で登録）。
- `llm/claude.py` が公式 SDK（`anthropic`）で Messages API を1回呼ぶ。SDK の型は外に出さない。指示（Prompt ファイル）は `system` に、Evidence など外部由来のデータを含む入力は `user` の JSON に分けて渡す。Tool は ToolBox 経由で、API の tool use は使わない。
- 出力の形は構造化出力で指定する（PR-10）。AI社員が作る `LLMRequest.response_schema`（出力モデルの JSON Schema）を `anthropic.transform_schema` で API が受け付ける形にし、`output_config.format`（`type: json_schema`）として送る。応答の JSON オブジェクトは `LLMResponse.structured` に入れる。「```json」の囲みを外す処理はせず、JSON オブジェクトでない応答は AI社員の検証で `validation_error` になる。スキーマは実行ごとに作る（`agents/base.py` の `output_schema_for`）：Evidence がある実行では `evidence_id` をその実行で入力した Evidence の ID の enum に限定し、Evidence が0件の実行（アイデア生成は常にこちら）では主張から `evidence_refs` を除き、`kind` を inference / speculation に限定する。スキーマで表せない規則（C-09 の組み合わせ、文字数・件数、入力外の Evidence 参照）は、Pydantic とステージ実行側の検証で引き続き確かめる。
- SDK の自動再試行はしない（`max_retries=0`）。1回の呼び出し＝1回の記録・計上にして、回数と費用の上限を正しく効かせるため。呼び出しの上限秒数（120秒）は呼び出しごとに渡す。出力の上限は 16,000 トークン（非ストリーミングの範囲）。
- 費用の上限（10章）：呼び出しの前に、残りの1実行あたりの費用上限で払える出力トークン数まで `max_tokens` を絞り（入力は system・メッセージ・構造化出力のスキーマの文字数で見積もる。`estimated_input_tokens`）、払えなければ呼ばずに `budget_exceeded` にする。断られた（refusal）・途中で切れた（max_tokens）応答も、使ったトークン分の費用を記録してから `llm_error` にする（E-07）。
- データ分類（11章）：送信上限は `LLM_MAX_CLASSIFICATION` で、指定がなければ internal（R-03。契約条件を確認するまで変えない）。restricted はどの LLM にも送らない。
- API キー（`LLM_API_KEY`、SecretStr）は SDK のクライアントにだけ渡す。環境の他の認証情報（`ANTHROPIC_API_KEY`・ログイン済みのプロファイル）は使わない。キーは LLM ログ・例外のメッセージ・監査ログに入らない。キーが未設定なら anthropic の AI社員は `llm_error` で失敗し、テスト・CI には影響しない。
- `APP_ENV=test` では実際のプロバイダーを使わない（テストや CI が実 API を呼ばないための安全装置）。テストは Fake LLM と、SDK を差し替えた Fake で行う。
- 接続確認は手動のワークフロー `.github/workflows/llm-smoke.yml`（`workflow_dispatch`、入力 `confirm` に `run`）だけで行う。Repository Secret `ANTHROPIC_API_KEY` を使って Claude API を2回だけ呼ぶ（通常の呼び出しと、本番と同じ経路での最小の構造化出力。2回目の `structured` が `{"ok": true}` でなければ失敗）。費用の上限は2回の合計で 0.05 USD（呼ぶ前に最悪の場合の費用、呼んだ後に実際の費用を確認）、5分で打ち切り、同時に1つだけ。送るのは固定の短い文で、業務データは送らない。

### Web 取得 Tool（第2回仕様 13章・R-20）

URL を指定して公開 Web ページを取得する `web_fetch`（external_read）だけを持ちます。検索 API の Tool は第2回では作りません。

- 取得結果はテキストにして Evidence 候補（原情報）として返す。人間が承認するまで AI の入力・根拠にならない。AI が書いた要約は補助情報として別に保存される（上記）。
- 接続先は HTTPS・ポート 443 だけ。URL に認証情報を含めない。IP アドレスを直接指定しない。
- SSRF 対策：名前解決したすべてのアドレスを検査し、1つでも公開アドレスでない（プライベート・ループバック・リンクローカル・メタデータ `169.254.169.254`・`fd00:ec2::254`・CGNAT・予約済み・IPv4 埋め込みの IPv6 など）なら拒否する。検査したアドレスにだけ接続し（名前解決をやり直さない）、TLS 証明書はホスト名で検証する。リダイレクト（最大3回）のたびに URL・ドメイン・アドレスを検査し直す。
- 上限：応答 5MB（Content-Length と読んだ量の両方）、1回の取得 15秒（リダイレクトを含む）、1実行あたり 20件。圧縮した応答は受け付けない。
- HTML はテキストを取り出すだけで、スクリプトは実行しない（script・style などは捨てる）。HTML とプレーンテキスト以外は受け付けない。
- robots.txt を尊重する（RFC 9309：4xx は許可、5xx・接続できない場合は不許可）。User-Agent を明示する（`WEB_FETCH_USER_AGENT`）。
- 秘密情報は使わない・送らない。Cookie・認証ヘッダーは送らず、応答ヘッダーも AI社員とログに渡さない。有料 API キーは持たない。
- 許可は「AI社員の `allowed_tools` × 副作用の区分（`TOOL_ALLOWED_SIDE_EFFECTS`。write は設定しても拒否）× 組織での有効化」。組織での有効化とドメインの許可・禁止リストは設定ファイル（`TOOL_CONFIG_PATH`、例：`config/tools.example.json`）で持ち、実行ごとに読み込む。設定ファイルがない組織は外部 Tool を使えない。

運用手順（Tool の設定）：
1. 取得したいサイトの利用規約と robots.txt を人間が確認する。
2. 確認したドメインだけを `allowed_domains` に追加する（`*` は検証環境以外では使わない）。社内・管理用のドメインは `blocked_domains` に入れる。
3. 組織の `enabled_tools` に `web_fetch` を入れ、AI社員の `allowed_tools` にも `web_fetch` を入れる（admin）。
4. 外部への通信はワーカーからだけ行う。本番ではワーカーを `EXECUTION_MODE=async` で動かし、API のプロセスからの外向き通信をネットワークの設定で閉じる（開発環境と CI では強制しない）。

### 費用管理（第2回仕様 10章）

- 単価は `pricing`（LLM はプロバイダー × モデル、Tool は名前。組織共通）。単価の変更は新しい行で行い、履歴を残す。呼び出しの時点の単価で費用を計算し、使った単価の ID を記録する。Fake LLM の単価（0 USD）は seed が登録する。
- 費用は LLM・Tool を呼ぶたびに `llm_calls` / `tool_calls` に記録してすぐ確定し、`executions.cost_amount` に加算する。取り消し・失敗・タイムアウトで終わった実行の費用も残り、予算に計上する（E-07）。通貨はプロバイダーの請求通貨のまま（R-21）。
- 予算は月単位（UTC の暦月）。組織全体（`budgets` に行がなければ設定の既定値：月額 100 USD、hard）と、探索案件ごと（任意）。hard は超えたら止める、soft は止めない。
- LLM の単価は seed で登録する（`claude-opus-5-5`、`claude-sonnet-5-5`、`claude-haiku-4-5`、`claude-haiku-4-5-20251001`。公式にある ID だけ）。AI社員の作成、provider か model が変わる更新、割り当ての作成で、解決後の（provider, model）に有効な単価がなければ 422。起動時に単価がなければ 409 で、API は呼ばない（第2回仕様 10章 SC候補-12）。model を省略したときの既定（`llm/factory.py` の `DEFAULT_MODELS`）は、anthropic が `claude-opus-5-5`、fake が `fake-model-v1`。
- 1実行あたりの上限は AI社員の `llm_config`（`max_cost_per_execution`、`max_llm_calls`、`max_tool_calls`、`max_tokens`）。未指定なら設定の既定値（1 USD、各20回）。受け付けた時点の費用上限を `executions.cost_limit` に残す。
- 起動時：各 AI社員の LLM に単価がない、単価の通貨が予算と違う場合は 409。残りの予算（上限 − 当月の費用 − 待機中・実行中の実行の確保分）が新しい実行の上限の合計より少なければ 409（`budget_exceeded`）。
- 実行中：LLM・Tool を呼ぶ前に、回数・費用の上限と当月の予算を確認し、超えていれば以降を止めて `failed`（`budget_exceeded`）。呼び出しの後に費用の上限を超えた場合も `failed` にする（その呼び出しの費用は記録する）。
- 運用：予算は `PUT /api/v1/budgets`（admin）で設定し、`GET /api/v1/costs?month=YYYY-MM` で当月の費用と予算の残りを確認する。実LLMの単価は、接続するとき（PR-9）に `pricing` に登録する。

### LLM・Tool のログ（第2回仕様 12章・14章）

- メタデータ（`llm_calls`・`tool_calls`）は永続。プロバイダー、モデル、Prompt の key / version / hash、トークン数、費用、応答時間、状態、エラー、プロバイダーのリクエストID、送ったデータの分類を持つ。`llm_calls.request_params` には、実際に送った temperature・thinking・effort・max_tokens を記録する（送っていない項目は `{"sent": false}`。失敗した呼び出しも含む。秘密情報は入れない。第2回仕様 11章 SC候補-9）。
- 本文（`llm_call_payloads`）は送ったメッセージと応答だけ。送ったデータの分類が public・internal なら保存し、confidential 以上は保存しない（R-16）。設定 `LLM_PAYLOAD_MODE=none` で全体を保存しないこともできる（既定より厳しくすることだけを許す）。閲覧は admin のみ。
- API キー・認証ヘッダーなどの秘密情報は、どのログにも保存しない（リクエストの本文から組み立て、ヘッダーは記録しない）。
- 本文の保存期間は 90日（R-20）。`make retention`（`python -m ai_business_explorer.retention`）を手動または cron から起動して消す。メタデータは残し、消した日時を `payload_deleted_at` に記録する。
- Tool の生の出力は `tool_call_outputs` に、取得した本文は Evidence 候補のスナップショットに保存する（上記）。

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
| Evidence 候補の承認・却下・一括承認 | member 以上 | `require_human` | 判断の記録は複合 FK + `CHECK (decided_by_actor_type = 'human')`。Tool 取得の Evidence は来歴（候補・Tool 呼び出し・実行・取得日時）が必須 |
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
| 入力 | `stage_runs.input_snapshot`、`executions.input`（使った Evidence と Analysis の ID、入力の最も高い分類） |

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

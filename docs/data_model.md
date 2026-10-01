# Data Model

全テーブル共通のルール：
- ID は UUIDv7（アプリ側で生成）。時刻は `timestamptz`（UTC）。
- 列挙値は文字列で保存し、CHECK 制約で値を制限する。ステージや AI社員のキーは文字列で持ち、コード側のレジストリで検証する。
- 識別子は英語の snake_case。データの内容は日本語を使える。

## リレーション

```
organizations 1─* (すべての業務テーブル。organization_id)
organizations 1─* organization_memberships *─1 actors (human のみ)
organizations 1─* stage_assignments *─1 ai_employees
explorations 1─* ideas
explorations 1─* stage_runs (idea_generation: idea_id = NULL)
ideas        1─* stage_runs (その他のステージ)
stage_runs   1─* executions *─1 ai_employees
executions   1─* analyses 1─* claims *─* evidence   (claim_evidence_links。正本)
analyses     *─* evidence                (analysis_evidence_links。第1回の履歴。凍結)
analyses     1─* ideas                   (ideas.origin_analysis_id: AI が生成した Idea)
analyses     1─* human_reviews *─0..1 claims (主張単位のレビュー)
ideas        1─* human_decisions
actors       1─* (作成者 / 起動者 / レビュアー / 決定者 / 監査ログ)
analyses.supersedes_id → analyses            (版の連鎖)
stage_runs.rerun_of_id / sent_back_from_id → stage_runs
```

## テーブル

| テーブル | 役割 | 主な制約 |
|---|---|---|
| `organizations` | 組織。第2回は既定組織（`00000000-0000-7000-8000-000000000100`）のみ | |
| `organization_memberships` | 人間の所属とロール | `role ∈ viewer/member/reviewer/admin`、複合 FK + `actor_type = 'human'`、`actor_id` は一意（1 actor = 1 組織） |
| `actors` | 操作者（`human` / `system`）。組織には memberships で所属する | `UNIQUE (id, actor_type)`（人間限定の複合 FK の参照先） |
| `ai_employees` | AI社員の定義（正本）。組織ごとに持つ | `(organization_id, key)` は一意、`status ∈ draft/active/inactive`、`version ≥ 1` |
| `stage_assignments` | ステージへのAI社員の割り当て | `role ∈ primary/secondary`、primary は組織×ステージごとに最大1人、`stage_key <> 'human_review'`、複合 FK（ai_employee_id, stage_key, organization_id）でAI社員の担当ステージ・組織と一致 |
| `explorations` | 探索案件 | `status ∈ active/archived`、`classification ∈ public/internal/confidential/restricted`（既定 internal。Idea はこの分類に従う） |
| `ideas` | 事業アイデア（仕様書9章の項目はすべて自由記述テキスト） | `adoption_status ∈ candidate/adopted/rejected`、`origin_type = 'ai'` と `origin_analysis_id IS NOT NULL` が同値 |
| `stage_runs` | ステージ実行の試行。ワーカーの記録（`claimed_at`、`worker_id`、`heartbeat_at`）を持つ | ステージと範囲の整合（idea_generation なら idea_id は NULL）、起動者は human、試行番号は一意、**最新（未 supersede）の試行は範囲×ステージごとに1つ**（部分一意インデックス）、`status` |
| `executions` | AI社員の実行履歴（入力、出力、エラー、使用量、費用の合計 `cost_amount`・`cost_currency`、受付時の上限 `cost_limit`、各種バージョン）。`assignment_role` は primary / secondary | `status`、`error_type`、`assignment_role ∈ primary/secondary`、**primary は stage_run ごとに1つ**（部分一意インデックス）、同じ AI社員は stage_run ごとに1回、`started_at` は queued の間は NULL |
| `analyses` | AI Analysis（本体は不変） | `review_status`、`version_no ≥ 1`、`execution_id NOT NULL`、`classification`（入力の最も高い分類。算出値）、`ai_employee_id` は実行の AI社員と一致（複合 FK）、**版番号は範囲×ステージ×AI社員ごとに一意** |
| `evidence` | 根拠・出典（不変。来歴 `acquisition_method ∈ human_input/tool`。Tool 取得なら候補・Tool 呼び出し・実行・取得日時が必須（CHECK）、候補ごとに1つ。訂正は撤回＋新規登録。本文の消去だけは記録付きで可） | `source_type` に `ai_generated` を含めない、撤回日時と撤回理由は必ずセット、更新版の連鎖（`supersedes_evidence_id`）は一意・自分自身を指さない・同じ組織、消去の記録（日時・理由・消去した人間）は必ずセットで、消去した人は human のみ（複合 FK + CHECK）、`classification`（既定 internal。登録後は変えない） |
| `claims` | AI Analysis の主張（正本。不変） | `kind ∈ evidence_based/inference/speculation`、`(analysis_id, claim_key)` は一意 |
| `claim_evidence_links` | 主張と Evidence の関係（正本。不変） | 主キーは `(claim_id, evidence_id, relation)`、`relation ∈ supports/contradicts/context`、主張・Evidence と同じ組織（複合 FK） |
| `pricing` | 単価表（LLM・Tool。組織共通。変更は新しい行で） | `kind ∈ llm/tool`、単価 ≥ 0、`currency` は3文字の大文字、`(kind, provider, model, effective_from)` は一意 |
| `budgets` | 月額予算（組織全体、または探索案件） | 組織・探索案件ごとに1行（部分一意インデックス）、`mode ∈ hard/soft`、`monthly_limit ≥ 0`、探索案件と同じ組織（複合 FK） |
| `llm_calls` | LLM 呼び出しのメタデータ（永続） | 実行と同じ組織（複合 FK）、`status ∈ succeeded/failed`、`payload_mode ∈ full/none`、`classification`、費用・トークン数 ≥ 0 |
| `llm_call_payloads` | LLM 呼び出しの本文（admin のみ閲覧。90日で消す） | `llm_call_id` が主キー、呼び出しと同じ組織（複合 FK） |
| `tool_calls` | Tool 呼び出しのメタデータ（永続） | 実行と同じ組織（複合 FK）、`status`、費用 ≥ 0 |
| `evidence_candidates` | Tool が取得した Evidence 候補（原情報だけ。AI生成の文章は持たない） | `status ∈ pending/accepted/rejected/duplicate`、承認・却下は人間の判断の記録が必須（複合 FK + CHECK）、却下は理由必須、duplicate は重複先が必須、抜粋は 2,000字以内、実行・Tool 呼び出し・探索案件と同じ組織（複合 FK） |
| `evidence_candidate_ai_notes` | AI生成の補助情報（Evidence ではない。不変） | 元の候補と生成した実行を必ず参照（複合 FK）。LLM のモデル・Prompt の版を記録 |
| `tool_call_outputs` | Tool の生の出力（90日で消す） | `tool_call_id` が主キー、呼び出しと同じ組織（複合 FK） |
| `analysis_evidence_links` | 第1回の根拠リンク（履歴。凍結） | INSERT / UPDATE / DELETE / TRUNCATE を DB トリガーで拒否。アプリも書き込まない |
| `human_reviews` | 人間のレビュー（追記のみ）。`claim_id` を指定すると主張単位のレビュー | 複合 FK + `reviewer_actor_type = 'human'`、`decision ∈ approve/reject/request_changes/needs_more_evidence`、`claim_id` は対象の分析の主張（複合 FK） |
| `human_decisions` | 人間の最終判断（追記のみ） | 複合 FK + `decided_by_actor_type = 'human'`、`decision ∈ go/no_go/hold/pivot` |
| `audit_events` | 監査ログ | `(entity_type, entity_id)` にインデックス |

業務テーブル（`ai_employees`、`explorations`、`ideas`、`stage_runs`、`executions`、`analyses`、`claims`、`claim_evidence_links`、`evidence`、`human_reviews`、`human_decisions`、`audit_events`、`stage_assignments`）はすべて `organization_id` を持ちます。親から分かる場合も冗長に持ち、親子の組織の一致は複合 FK（子の `(親ID, organization_id)` → 親の `(id, organization_id)`）で保証します。

### Evidence の状態（第2回 E-01）

状態は保存せず、取得時に算出します。superseded・retracted・purged は別の概念です。

| 状態 | 条件 | 新規Analysisの入力 |
|---|---|---|
| active | 下のどれにも当たらない | 入力する |
| superseded | 更新版（`supersedes_evidence_id` でこの行を指す Evidence）がある | 入力しない |
| retracted | 人間が理由を付けて撤回した | 入力しない |
| purged | 本文（`quote`・`summary`）を消去した（admin のみ。理由必須） | 入力しない |

複数に当てはまる場合の表示は purged > retracted > superseded > active の順で、元の各状態も `is_retracted` / `is_superseded` / `is_purged` で返します。どの状態でも行・ID・出典・ハッシュ・来歴・過去の分析の根拠リンクは残り、過去の分析は書き換えません。根拠リンクの応答には、参照先の現在の状態（`evidence_status` など）を付けます。

`source_key` は出典の同一性です（Web は URL のスキームとホストを小文字にし、`#` 以降と追跡用パラメータを除いたもの）。人間が登録した Evidence が、同じ探索案件の active な Evidence と出典（`source_key`）か内容（`content_hash`）で重なる場合は、拒否せず応答の `warnings` で知らせます。第1回の既存の Evidence の `source_key` は空です。

### Evidence の項目（仕様書11章との対応）

| 仕様書 | 列 |
|---|---|
| Evidence ID | `id` |
| 調査結果ID | `claim_evidence_links` → `claims.analysis_id`（多対多。1つの Evidence を複数の分析・主張から参照できる） |
| 出典タイトル / URL / 出典タイプ | `title` / `url` / `source_type` |
| 引用・要約 | `quote`（原文の引用）/ `summary`（人間が書いた要約。AI の要約は analyses 側に置く） |
| 取得日時 / 情報の発生日 | `retrieved_at` / `published_at` |
| その他メタデータ | `metadata`（JSONB） |

### AI Analysis の主張と本体

主張と根拠は `claims` / `claim_evidence_links` が正本です。`analyses.body` は「Analysis 生成時点のAI出力スナップショット」として残し、書き換えません。両者が食い違った場合は `claims` が正しいものとします。

`analyses.body`（スナップショット）の形：

```json
{
  "claims": [
    {"id": "e1", "text": "...", "kind": "evidence_based",
     "evidence_refs": [{"evidence_id": "...", "relation": "supports"}]},
    {"id": "i1", "text": "...", "kind": "inference", "evidence_refs": []}
  ],
  "data": { "...": "AI社員ごとの構造化出力（schema_version で区別）" }
}
```

`kind`（`evidence_based` / `inference` / `speculation`）で主張の性質を区別します。`evidence_based` は「Evidence によって真偽が評価された主張」で、Evidence に否定された主張も含みます。

`relation` は、主張（claim）の内容と Evidence の関係です（Idea の前提との関係ではありません）。

- `supports`：Evidence がその主張の内容を支持する。
- `contradicts`：Evidence がその主張の内容を否定する。
- `context`：Evidence は主張の真偽を直接支持・否定せず、前提・背景などの文脈を提供する。

AI の出力は次の規則で検証し、違反した場合は実行を `failed`（`validation_error`）にして何も保存しません（第2回仕様 C-09）。

- relation は必須。省略した出力を supports とみなさない。

- 同じ主張と Evidence に、同じ relation を重複して付けない。
- 同じ主張と Evidence に、supports と contradicts を同時に付けない。
- `evidence_based` の主張は supports か contradicts を最低1つ持つ。context だけでは根拠にならない（context は supports・contradicts と並べてよい）。

## 状態遷移

- **Idea**
  - 採否：`candidate → adopted` / `candidate → rejected`。人間のみが実行でき、一方向の遷移。
  - 最終判断：`human_decisions` は `adopted` の Idea にだけ記録できる（candidate → adopt → 調査・分析 → Human Review → Human Decision）。
  - 調査：`current_stage_key` は、最新かつ成功した試行のうち最も後ろのステージ。
- **stage_runs / executions**：`queued → running → succeeded | failed`。`queued` と `running` は人間が `cancelled` にできる。heartbeat が途絶えた `running` は `failed`。自動の再実行はしない。
  - stage_run の状態は primary の execution で決まる。secondary の失敗では stage_run を失敗にしない。
  - `queued` / `running` の試行が残っている間は、その試行を置き換える再実行・差し戻しを受け付けない（409）。
  - 再実行：`rerun_of_id` に現在の最新試行を指定する。
  - 差し戻し：`send_back` を使う。現在のステージより前のステージを指定し、理由が必須。
  - どちらの場合も、対象ステージ以降の最新試行に `superseded_at` を記録する。
- **evidence_candidates**：`pending → accepted / rejected`（人間、member 以上）。`duplicate` は重複判定で自動設定（候補の作成時と承認時）。duplicate を pending に戻す操作は第2回では作らない（E-03）。
- **stage_runs.mode**：`collect_only` の試行は `trigger = initial` だけで、superseded にならず、「最新の試行は1つ」の部分一意インデックスにも数えない。
- **analyses.review_status**：`pending_review` から、最新のレビュー（`claim_id` のないもの）に応じて `approved` / `rejected` / `changes_requested` / `needs_more_evidence` に変わる。主張単位のレビュー（`claim_id` あり）は `review_status` を変えず、分析の応答の主張ごとに最新のレビューとして表示する。

## マイグレーション

`migrations/versions/` を参照してください。

- `0001`：第1回のスキーマ。`ideas.origin_analysis_id` と `analyses` は循環参照になるため、両テーブルを作った後で FK を追加しています。
- `0002`：組織とロール。既定組織を作り、第1回のすべての行をその組織に移します。既存の人間 actor は admin になり、system actor にはロールを付けません。組織×ステージごとに最も古い active のAI社員を primary にします。downgrade は開発用で、複数の組織に同じ key のAI社員がある場合は失敗します。
- `0003`：主張と根拠リンク。第1回の `analyses.body.claims` から `claims` を、`analysis_evidence_links` から `claim_evidence_links` を作ります（`body` は変えない）。旧リンクを1件でも取りこぼす場合は移行を中止します。移行の後、`analysis_evidence_links` を DB トリガーで凍結します。downgrade は開発用で、0003 以降に作った分析の主張と根拠リンクは失われます。
- `0004`：Evidence の版と消去の列（`source_key`、`snapshot_hash`、`supersedes_evidence_id`、消去の記録）と、一覧のカーソル方式のための複合インデックス（組織・親のID・作成日時（stage_runs は開始日時）・ID）。
- `0005`：非同期実行。stage_runs / executions の状態に `queued` と `cancelled` を追加し、ワーカーの列と `executions.assignment_role`（既存はすべて primary）を追加します。`analyses.ai_employee_id` を実行から埋め、版の連鎖を AI社員単位にします（既存の版番号は変えない）。downgrade は開発用で、`queued` / `cancelled` は `failed` に、未開始の実行の開始日時は作成日時になります。
- `0006`：データ分類。`explorations`・`evidence`・`analyses` に `classification` を追加します。既存の行はすべて `internal`（既存の分析の入力もすべて internal のため、算出値としても正しい）。downgrade は開発用で、分類の記録は失われます。
- `0007`：費用管理と LLM・Tool のログ。`pricing`・`budgets`・`llm_calls`・`llm_call_payloads`・`tool_calls` を作り、`executions` に費用の合計と上限を追加します（既存の実行は 0 USD・上限なし）。`error_type` に `budget_exceeded` を追加します。downgrade は開発用で、費用とログは失われ、`budget_exceeded` は `unexpected` になります。
- `0008`：Evidence 候補と収集のみ。`evidence_candidates`・`evidence_candidate_ai_notes`・`tool_call_outputs` を作り、`evidence` に来歴の列（既存はすべて `human_input`）、`stage_runs` に `mode`（既存はすべて `analyze`）を追加します。「最新の試行は1つ」の部分一意インデックスを analyze に限ります。downgrade は開発用で、候補・補助情報・来歴は失われ、collect_only の試行は superseded になります。

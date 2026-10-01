# Data Model

全テーブル共通のルール：
- ID は UUIDv7（アプリ側で生成）。時刻は `timestamptz`（UTC）。
- 列挙値は文字列で保存し、CHECK 制約で値を制限する。ステージや AI社員のキーは文字列で持ち、コード側のレジストリで検証する。
- 識別子は英語の snake_case。データの内容は日本語を使える。

## リレーション

```
explorations 1─* ideas
explorations 1─* stage_runs (idea_generation: idea_id = NULL)
ideas        1─* stage_runs (その他のステージ)
stage_runs   1─* executions *─1 ai_employees
executions   1─* analyses *─* evidence   (analysis_evidence_links)
analyses     1─* ideas                   (ideas.origin_analysis_id: AI が生成した Idea)
analyses     1─* human_reviews
ideas        1─* human_decisions
actors       1─* (作成者 / 起動者 / レビュアー / 決定者 / 監査ログ)
analyses.supersedes_id → analyses            (版の連鎖)
stage_runs.rerun_of_id / sent_back_from_id → stage_runs
```

## テーブル

| テーブル | 役割 | 主な制約 |
|---|---|---|
| `actors` | 操作者（`human` / `system`） | `UNIQUE (id, actor_type)`（人間限定の複合 FK の参照先） |
| `ai_employees` | AI社員の定義（正本） | `key` は一意、`status ∈ draft/active/inactive`、`version ≥ 1` |
| `explorations` | 探索案件 | `status ∈ active/archived` |
| `ideas` | 事業アイデア（仕様書9章の項目はすべて自由記述テキスト） | `adoption_status ∈ candidate/adopted/rejected`、`origin_type = 'ai'` と `origin_analysis_id IS NOT NULL` が同値 |
| `stage_runs` | ステージ実行の試行 | ステージと範囲の整合（idea_generation なら idea_id は NULL）、起動者は human、試行番号は一意、**最新（未 supersede）の試行は範囲×ステージごとに1つ**（部分一意インデックス） |
| `executions` | AI社員の実行履歴（入力、出力、エラー、使用量、各種バージョン） | `status`、`error_type` |
| `analyses` | AI Analysis（本体は不変） | `review_status`、`version_no ≥ 1`、`execution_id NOT NULL` |
| `evidence` | 根拠・出典（不変。訂正は撤回＋新規登録） | `source_type` に `ai_generated` を含めない、撤回日時と撤回理由は必ずセット |
| `analysis_evidence_links` | 主張（claim_ref）と Evidence の対応 | `relation ∈ supports/contradicts/context` |
| `human_reviews` | 人間のレビュー（追記のみ） | 複合 FK + `reviewer_actor_type = 'human'`、`decision ∈ approve/reject/request_changes/needs_more_evidence` |
| `human_decisions` | 人間の最終判断（追記のみ） | 複合 FK + `decided_by_actor_type = 'human'`、`decision ∈ go/no_go/hold/pivot` |
| `audit_events` | 監査ログ | `(entity_type, entity_id)` にインデックス |

### Evidence の項目（仕様書11章との対応）

| 仕様書 | 列 |
|---|---|
| Evidence ID | `id` |
| 調査結果ID | `analysis_evidence_links.analysis_id`（多対多。1つの Evidence を複数の分析から参照できる） |
| 出典タイトル / URL / 出典タイプ | `title` / `url` / `source_type` |
| 引用・要約 | `quote`（原文の引用）/ `summary`（人間が書いた要約。AI の要約は analyses 側に置く） |
| 取得日時 / 情報の発生日 | `retrieved_at` / `published_at` |
| その他メタデータ | `metadata`（JSONB） |

### AI Analysis の本体（`analyses.body`）

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

`kind`（`evidence_based` / `inference` / `speculation`）で主張の性質を区別します。`evidence_based` の主張は Evidence への参照が必須です。

## 状態遷移

- **Idea**
  - 採否：`candidate → adopted` / `candidate → rejected`。人間のみが実行でき、一方向の遷移。
  - 調査：`current_stage_key` は、最新かつ成功した試行のうち最も後ろのステージ。
- **stage_runs / executions**：`running → succeeded | failed`。
  - 再実行：`rerun_of_id` に現在の最新試行を指定する。
  - 差し戻し：`send_back` を使う。現在のステージより前のステージを指定し、理由が必須。
  - どちらの場合も、対象ステージ以降の最新試行に `superseded_at` を記録する。
- **analyses.review_status**：`pending_review` から、最新のレビューに応じて `approved` / `rejected` / `changes_requested` / `needs_more_evidence` に変わる。

## マイグレーション

`migrations/versions/` を参照してください。`ideas.origin_analysis_id` と `analyses` は循環参照になるため、両テーブルを作った後で FK を追加しています。

# AI Employee Specification

## 定義（DB：`ai_employees`、正本）

| 項目 | 列 | 備考 |
|---|---|---|
| ID / 名前 / 役割 / 説明 / 目的 | `id` / `name` / `role` / `description` / `purpose` | |
| 担当ステージ | `stage_key` | `human_review` には割り当てられない |
| 実装 | `implementation_key` | コード側のレジストリのキー。NULL なら定義のみで、実行できない |
| 使用モデル | `llm_config` | `{"provider": "fake", "model": "fake-model-v1"}` または `{"provider": "anthropic"}`（Claude API。model の既定は `claude-opus-5-5`）。1実行あたりの上限 `max_tokens`・`max_llm_calls`・`max_tool_calls`・`max_cost_per_execution` も持てる |
| 使用ツール | `allowed_tools` | 許可リスト。第1回は本番用ツールがない |
| 入力形式 / 出力形式 | `input_format` / `output_format` | JSON Schema。実装がある場合は省略すると自動で設定される |
| Prompt | `prompt_key` / `prompt_version` | `prompts/<key>/<version>.md` |
| ステータス | `status` | `draft` / `active` / `inactive`（削除はせず無効化する） |
| バージョン | `version` | 更新のたびに +1。実行時のスナップショットは executions に保存 |

AI社員の定義は組織ごとに持ち、`key` は組織の中で一意です。ステージへの割り当ては `stage_assignments`（primary / secondary）で持ちます。

API：`POST/GET /api/v1/ai-employees`、`GET/PATCH /api/v1/ai-employees/{id}`（書き込みは admin のみ）。

## 実装（コード）

```python
class Agent(ABC):
    implementation_key: ClassVar[str]  # ai_employees.implementation_key と対応
    stage_key: ClassVar[str]  # 担当ステージ（DB 定義と一致が必要）
    output_schema_version: ClassVar[str]  # analyses.schema_version に記録
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]

    def run(self, ctx: AgentContext) -> AnalysisDraft: ...
```

`AgentContext` で使えるのは次のものだけです。

| 項目 | 内容 |
|---|---|
| 入力データ（読み取り専用） | `exploration`、`idea`、`evidence`、`prior_analyses`、`research_question` |
| `llm` | 使用量とモデルが自動で集計される |
| `tools` | 許可リストと副作用ポリシーを強制する |
| `prompt` | key、version、本文、SHA-256 |
| `mode` | `analyze`（分析を作る）/ `collect_only`（Tool で Evidence 候補を集めるだけ。分析は保存されない） |

AI社員は DB・Repository・サービスに触れられません。このため、Human Review、Human Decision、再実行、差し戻し、Idea の採否を起動することはできません。

`AnalysisDraft` に含めるもの：
- `summary`
- `claims`（`kind` で、Evidence によって真偽が評価された主張（`evidence_based`）／推論（`inference`）／推測（`speculation`）を区別する。違反すると `validation_error`）
  - relation（必須）は、主張（claim）の内容と Evidence の関係を表す。Idea の前提との関係ではない。supports＝Evidence がその主張の内容を支持する、contradicts＝Evidence がその主張の内容を否定する、context＝主張の真偽を直接支持・否定せず、前提・背景などの文脈を提供する。
  - `evidence_based` は supports か contradicts の Evidence 参照が1つ以上必要（否定された主張も含む）。context だけでは `evidence_based` にならない。
  - 同じ Evidence への同じ relation の重複と、supports・contradicts の同時指定は不可。relation を省いた出力は supports とみなさず、`validation_error` にする。
- `data`（構造化出力）
- `idea_candidates`（idea_generation のみ。AI が書けるのは title / summary / problem だけ）
- `candidate_notes`（AI生成の補助情報。この実行で Tool が返した候補の `candidate_id` を指す。Evidence 候補・Evidence とは別のテーブルに保存され、Evidence に移ることはない。他の実行の候補を指すと `validation_error`）

使える Tool は `web_fetch`（URL を指定して公開 Web ページを取得する。external_read）です。AI社員の `allowed_tools` に入れ、組織の設定ファイルで有効にしたときだけ使えます（[Architecture](architecture.md) の「Web 取得 Tool」）。結果（`ToolResult.output`）には最終 URL・状態・種類・タイトル・文字数だけが入り、応答ヘッダーは入りません。

Tool の結果（`ToolResult.evidence_candidates`）は、仕組みが Evidence 候補として保存し、`candidate_id` を付けて AI社員に返します。候補に入れてよいのは Tool が外部から取得した原情報（URL、title、取得日時、メタデータ、抜粋・全文）だけです。未承認の候補は AI の入力にも根拠にもならず、`evidence_refs` に候補の ID を指定すると `validation_error` になります。

## 第1回の AI社員（Fake）

| key | 担当ステージ | 内容 |
|---|---|---|
| `idea_generator` | idea_generation | テーマから Idea 候補を3件生成する（`candidate` で登録） |
| `market_researcher` | market_research | Evidence によって真偽を評価した主張を `evidence_based` とし、根拠のない推論は `inference` として明示する。Prompt は v2（relation の意味を明記。v1 は変更せず残す） |

## AI社員の追加手順

1. `src/ai_business_explorer/agents/employees/<name>.py` に `Agent` のサブクラスを作る
2. `src/ai_business_explorer/prompts/<prompt_key>/v1.md` を作る
3. `agents/registry.py` の `build_default_registry()` に登録する
4. （Fake で動かす場合）`llm/fake.py` の `DEFAULT_RESPONDERS` に応答を追加し、テストを書く
5. API（`POST /api/v1/ai-employees`）または seed で定義を登録し、`status=active` にする

DB マイグレーションは不要です。AI 出力は `analyses.body`（JSONB。生成時点のスナップショット）に保存し、`schema_version` で区別します。主張と根拠は共通の `claims` / `claim_evidence_links` に保存されます。

同じステージに有効な AI社員が複数いる場合は、ステージ実行時に `ai_employee_id` で指定します。

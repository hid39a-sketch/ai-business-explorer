-- 04 スモークテスト後の確認（読み取り専用）
--   psql -v ON_ERROR_STOP=1 -v since="'2026-10-02 00:00+09'" -f 04_post_smoke_check.sql
--   since：スモークテストを始めた日時
BEGIN TRANSACTION READ ONLY;

-- (a) LLM 呼び出し。期待値：status=succeeded、request_params が null でない、
--     cost_amount > 0 で pricing_id あり（anthropic）、prompt_version は v2/v4
SELECT c.created_at, c.provider, c.model, c.prompt_key, c.prompt_version, c.status,
       c.error_type, c.input_tokens, c.output_tokens, c.cost_amount, c.currency,
       c.pricing_id IS NOT NULL AS has_pricing, c.latency_ms, c.provider_request_id,
       c.request_params
FROM llm_calls c
WHERE c.created_at >= :since::timestamptz
ORDER BY c.created_at;

-- (b) 分析の出力契約。期待値：idea_generation.v2 / market_research.v2、claims は10件以下
SELECT a.created_at, a.schema_version, jsonb_array_length(coalesce(a.body->'claims', '[]')) AS claims,
       jsonb_array_length(coalesce(a.body->'data'->'ideas', '[]')) AS ideas
FROM analyses a
WHERE a.created_at >= :since::timestamptz
ORDER BY a.created_at;

-- (c) 実行の状態
SELECT x.created_at, x.status, x.prompt_key, x.prompt_version, x.llm_provider
FROM executions x
WHERE x.created_at >= :since::timestamptz
ORDER BY x.created_at;

ROLLBACK;

-- 02 LLM 単価の確認（読み取り専用）。seed の前後で実行する
BEGIN TRANSACTION READ ONLY;

WITH expected(provider, model, input_price, output_price) AS (
  VALUES
    ('fake',      'fake-model-v1',             0::numeric,  0::numeric),
    ('anthropic', 'claude-opus-5-5',           4::numeric, 20::numeric),
    ('anthropic', 'claude-sonnet-5-5',         2::numeric, 10::numeric),
    ('anthropic', 'claude-haiku-4-5',          1::numeric,  5::numeric),
    ('anthropic', 'claude-haiku-4-5-20251001', 1::numeric,  5::numeric)
),
current_price AS (
  SELECT DISTINCT ON (provider, model)
         provider, model, input_per_million_tokens, output_per_million_tokens,
         per_call, currency, effective_from
  FROM pricing
  WHERE kind = 'llm' AND effective_from <= now()
  ORDER BY provider, model, effective_from DESC
)
SELECT e.provider, e.model, e.input_price AS expected_in, e.output_price AS expected_out,
       c.input_per_million_tokens AS db_in, c.output_per_million_tokens AS db_out,
       c.currency, c.effective_from,
       CASE
         WHEN c.model IS NULL THEN 'MISSING（seed で追加される）'
         WHEN c.input_per_million_tokens = e.input_price
          AND c.output_per_million_tokens = e.output_price
          AND c.per_call = 0 AND c.currency = 'USD' THEN 'OK'
         ELSE 'DIFFERENT（seed は既存行を変えない。停止して確認）'
       END AS result
FROM expected e
LEFT JOIN current_price c USING (provider, model)
ORDER BY e.provider, e.model;

ROLLBACK;

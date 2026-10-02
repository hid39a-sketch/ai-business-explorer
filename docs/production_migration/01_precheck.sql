-- 01 事前確認（読み取り専用）。psql -v ON_ERROR_STOP=1 -f 01_precheck.sql
BEGIN TRANSACTION READ ONLY;

-- (a) 現在の migration。期待値：0008（0009 適用前）
SELECT version_num AS alembic_version FROM alembic_version;

-- (b) llm_calls.request_params の有無。期待値：0行（0009 適用前）
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = current_schema() AND table_name = 'llm_calls' AND column_name = 'request_params';

-- (c) 件数（適用後の比較用）
SELECT 'llm_calls' AS t, count(*) FROM llm_calls
UNION ALL SELECT 'ai_employees', count(*) FROM ai_employees
UNION ALL SELECT 'stage_assignments', count(*) FROM stage_assignments
UNION ALL SELECT 'pricing', count(*) FROM pricing;

-- (d) 実行中・待機中の実行。期待値：0行（あれば移行を始めない）
SELECT id, status, created_at FROM executions WHERE status IN ('queued', 'running');

ROLLBACK;

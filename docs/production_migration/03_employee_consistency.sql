-- 03 既存 AI社員の整合性（読み取り専用）。データは変更しない
--   psql -v ON_ERROR_STOP=1 -v default_provider=anthropic -f 03_employee_consistency.sql
--   default_provider：本番の LLM_PROVIDER の値（未設定なら fake）
-- 出力契約の表は第2回仕様 17章と agents/employees/*.py の output_contracts に合わせてある
BEGIN TRANSACTION READ ONLY;

WITH e AS (
  SELECT e.*,
         coalesce(nullif(e.llm_config->>'provider', ''), :'default_provider') AS r_provider
  FROM ai_employees e
),
r AS (
  SELECT e.*,
         coalesce(nullif(e.llm_config->>'model', ''),
                  CASE e.r_provider WHEN 'anthropic' THEN 'claude-opus-5-5'
                                    WHEN 'fake' THEN 'fake-model-v1' END) AS r_model,
         CASE e.implementation_key WHEN 'idea_generator' THEN 'v2'
                                   WHEN 'market_researcher' THEN 'v4' END AS target_version
  FROM e
)
SELECT r.organization_id, r.key, r.id, r.stage_key, r.status, r.version,
       r.implementation_key, r.prompt_key, r.prompt_version,
       r.r_provider AS provider, r.r_model AS model,
       CASE
         WHEN r.implementation_key IS NULL THEN 'no_implementation'
         WHEN r.implementation_key NOT IN ('idea_generator', 'market_researcher')
           THEN 'UNKNOWN_IMPLEMENTATION'
         WHEN r.prompt_key IS NULL OR r.prompt_version IS NULL THEN 'PROMPT_MISSING'
         WHEN r.prompt_key <> r.implementation_key THEN 'MISMATCH（経過措置：v1 契約で実行）'
         ELSE 'match'
       END AS prompt_key_check,
       CASE
         WHEN r.implementation_key = 'idea_generator' AND r.prompt_key <> 'idea_generator'
           THEN 'idea_generation.v1（legacy_mismatch）'
         WHEN r.implementation_key = 'market_researcher' AND r.prompt_key <> 'market_researcher'
           THEN 'market_research.v1（legacy_mismatch）'
         WHEN r.implementation_key = 'idea_generator' AND r.prompt_version = 'v1'
           THEN 'idea_generation.v1'
         WHEN r.implementation_key = 'idea_generator' AND r.prompt_version = 'v2'
           THEN 'idea_generation.v2'
         WHEN r.implementation_key = 'market_researcher' AND r.prompt_version IN ('v1', 'v2', 'v3')
           THEN 'market_research.v1'
         WHEN r.implementation_key = 'market_researcher' AND r.prompt_version = 'v4'
           THEN 'market_research.v2'
         WHEN r.implementation_key IS NULL THEN '-'
         ELSE 'NO_CONTRACT（実行できない）'
       END AS current_contract,
       r.output_format -> 'properties' -> 'claims' ? 'maxItems' AS output_format_has_claims_max,
       CASE
         WHEN r.target_version IS NULL THEN 'not_applicable'
         WHEN r.prompt_key = r.implementation_key AND r.prompt_version = r.target_version
           THEN 'already_target'
         WHEN r.stage_key NOT IN ('idea_generation', 'market_research') THEN 'STAGE_MISMATCH'
         WHEN r.prompt_key IS DISTINCT FROM r.implementation_key
           THEN 'switchable（PATCH で prompt_key も実装に合わせる）'
         ELSE 'switchable'
       END AS switch_to_target,
       r.target_version,
       EXISTS (
         SELECT 1 FROM pricing p
         WHERE p.kind = 'llm' AND p.provider = r.r_provider AND p.model = r.r_model
           AND p.effective_from <= now()
       ) AS has_pricing,
       (SELECT string_agg(a.stage_key || ':' || a.role, ', ' ORDER BY a.role)
        FROM stage_assignments a WHERE a.ai_employee_id = r.id) AS assignments
FROM r
ORDER BY r.organization_id, r.stage_key, r.key;

-- 集計
SELECT implementation_key, prompt_key, prompt_version, status, count(*)
FROM ai_employees
GROUP BY 1, 2, 3, 4
ORDER BY 1, 2, 3, 4;

-- primary の割り当て（AI が実行するステージごと）。期待値：両ステージとも primary が1人いて active。
-- primary がいない・inactive だと、起動は 409（API は呼ばない）
WITH stages(stage_key) AS (VALUES ('idea_generation'), ('market_research'))
SELECT o.id AS organization_id, s.stage_key, e.key AS primary_key, e.status AS primary_status,
       e.prompt_key, e.prompt_version,
       CASE WHEN e.id IS NULL THEN 'NO_PRIMARY'
            WHEN e.status <> 'active' THEN 'PRIMARY_NOT_ACTIVE'
            ELSE 'ok' END AS primary_check
FROM organizations o
CROSS JOIN stages s
LEFT JOIN stage_assignments a
  ON a.organization_id = o.id AND a.stage_key = s.stage_key AND a.role = 'primary'
LEFT JOIN ai_employees e ON e.id = a.ai_employee_id
ORDER BY o.id, s.stage_key;

ROLLBACK;

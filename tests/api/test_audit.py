"""監査ログ（最小限）: 更新・状態遷移・レビュー・決定・差し戻しが記録される。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_business_explorer.infrastructure.db.models import AuditEvent
from tests.conftest import Api


def test_audit_events_are_recorded(api: Api, session: Session) -> None:
    exp = api.exploration()
    api.patch(f"/explorations/{exp['id']}", {"description": "更新"})
    idea = api.adopted_idea(exp["id"])
    api.patch(f"/ideas/{idea['id']}", {"summary": "要約"})
    run = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    analysis_id = run["executions"][0]["output"]["analysis_id"]
    api.post(f"/analyses/{analysis_id}/human-reviews", {"decision": "approve"})
    api.post(f"/ideas/{idea['id']}/human-decisions", {"decision": "go", "rationale": "妥当"})

    events = session.scalars(select(AuditEvent).order_by(AuditEvent.created_at)).all()
    pairs = {(e.entity_type, e.action) for e in events}
    assert {
        ("exploration", "created"),
        ("exploration", "updated"),
        ("idea", "created"),
        ("idea", "adoption_status.adopted"),
        ("idea", "updated"),
        ("stage_run", "started"),
        ("stage_run", "succeeded"),
        ("analysis", "reviewed"),
        ("idea", "human_decision"),
    } <= pairs
    update = next(e for e in events if (e.entity_type, e.action) == ("idea", "updated"))
    assert update.before == {"summary": None}
    assert update.after == {"summary": "要約"}

"""ドメインで使う列挙値。DB には文字列として保存し、CHECK 制約で値を制限する。"""

from enum import StrEnum


class ActorType(StrEnum):
    HUMAN = "human"
    SYSTEM = "system"


class AIEmployeeStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    INACTIVE = "inactive"


class ExplorationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class OriginType(StrEnum):
    HUMAN = "human"
    AI = "ai"


class AdoptionStatus(StrEnum):
    CANDIDATE = "candidate"
    ADOPTED = "adopted"
    REJECTED = "rejected"


class StageRunTrigger(StrEnum):
    INITIAL = "initial"
    RERUN = "rerun"
    SEND_BACK = "send_back"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ErrorType(StrEnum):
    VALIDATION_ERROR = "validation_error"
    LLM_ERROR = "llm_error"
    TOOL_ERROR = "tool_error"
    TIMEOUT = "timeout"
    STORAGE_ERROR = "storage_error"
    UNEXPECTED = "unexpected"


class ClaimKind(StrEnum):
    EVIDENCE_BASED = "evidence_based"
    INFERENCE = "inference"
    SPECULATION = "speculation"


class EvidenceSourceType(StrEnum):
    """Evidence の出典タイプ。AI 生成情報（ai_generated）は意図的に存在しない。"""

    HUMAN_INPUT = "human_input"
    DOCUMENT = "document"
    WEB = "web"
    API = "api"
    PATENT_DB = "patent_db"
    OTHER = "other"


# 第1回で API から登録できる出典タイプ（自動取得系は将来拡張）。
EVIDENCE_SOURCE_TYPES_ENABLED: frozenset[EvidenceSourceType] = frozenset(
    {EvidenceSourceType.HUMAN_INPUT, EvidenceSourceType.DOCUMENT}
)


class EvidenceRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXT = "context"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"


class ReviewStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"


REVIEW_STATUS_BY_DECISION: dict[ReviewDecision, ReviewStatus] = {
    ReviewDecision.APPROVE: ReviewStatus.APPROVED,
    ReviewDecision.REJECT: ReviewStatus.REJECTED,
    ReviewDecision.REQUEST_CHANGES: ReviewStatus.CHANGES_REQUESTED,
    ReviewDecision.NEEDS_MORE_EVIDENCE: ReviewStatus.NEEDS_MORE_EVIDENCE,
}


class HumanDecisionValue(StrEnum):
    GO = "go"
    NO_GO = "no_go"
    HOLD = "hold"
    PIVOT = "pivot"


def sql_in(values: type[StrEnum] | frozenset[StrEnum] | list[StrEnum]) -> str:
    """CHECK 制約用の `IN (...)` リストを生成する。"""
    return ", ".join(f"'{v.value}'" for v in values)

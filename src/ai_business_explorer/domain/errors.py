"""ドメイン例外。API 層で HTTP ステータスに変換される。"""


class DomainError(Exception):
    """ドメイン例外の基底。"""


class NotFoundError(DomainError):
    pass


class AuthenticationRequiredError(DomainError):
    """操作者（actor）を特定できない。"""


class PermissionDeniedError(DomainError):
    """操作者に権限がない（例：人間専用の操作を人間以外が実行）。"""


class InvalidStateError(DomainError):
    """現在の状態では実行できない操作。"""


class DomainValidationError(DomainError):
    """入力値がドメインルールに反する。"""


class BudgetExceededError(InvalidStateError):
    """予算または実行ごとの上限を超える（第2回仕様 10章。起動時は 409、実行中は failed）。"""

    def __init__(self, message: str) -> None:
        super().__init__(f"budget_exceeded: {message}")

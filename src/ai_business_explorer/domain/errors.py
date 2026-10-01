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

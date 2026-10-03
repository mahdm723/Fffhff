from __future__ import annotations


class AppError(Exception):
    """Business error returned to clients as {"error": {code, message, ...}}.

    `message` is user-facing (Arabic) and must never contain internal data.
    """

    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


def not_found() -> AppError:
    # Same answer whether the object does not exist or belongs to someone else.
    return AppError(404, "not_found", "غير موجود.")


def rate_limited(retry_after: float, message: str = "محاولات كثيرة. حاول مرة أخرى بعد قليل.") -> AppError:
    return AppError(429, "rate_limited", message, retry_after=max(1, int(retry_after + 0.999)))

"""领域错误与 API 错误码。

所有错误携带稳定的机器可读 code，便于调用方与合规审计区分
"被风险规则拒绝" 与 "参数错误/未授权"。
"""


class DomainError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


def not_found(message: str = "资源不存在", details=None) -> DomainError:
    return DomainError("not_found", message, 404, details)


def conflict(message: str, details=None) -> DomainError:
    return DomainError("conflict", message, 409, details)


def validation_error(message: str, details=None) -> DomainError:
    return DomainError("invalid_request", message, 400, details)


def consent_required(scope: str, details=None) -> DomainError:
    return DomainError(
        "consent_required",
        f"该操作需要客户在有效授权范围内同意：{scope}",
        403,
        details or {"required_scope": scope},
    )


def forbidden(message: str, details=None) -> DomainError:
    return DomainError("forbidden", message, 403, details)

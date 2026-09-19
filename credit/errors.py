"""领域错误与 HTTP 状态码的映射。"""


class DomainError(Exception):
    status = 400
    code = "domain_error"

    def __init__(self, message=None, *, details=None, status_code=None, code=None):
        super().__init__(message or (code or self.code))
        self.details = details or {}
        if status_code is not None:
            self.status = status_code
        if code is not None:
            self.code = code


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Conflict(DomainError):
    status = 409
    code = "conflict"


class InsufficientAvailable(Conflict):
    code = "insufficient_available"


class ValidationFailed(DomainError):
    status = 422
    code = "validation_failed"


class ConsentRequired(DomainError):
    status = 403
    code = "consent_required"


class AuthorizationDenied(DomainError):
    status = 403
    code = "authorization_denied"


class RiskHeld(Conflict):
    """提款被风险规则拦截，未放款部分已暂停。"""

    code = "risk_held"

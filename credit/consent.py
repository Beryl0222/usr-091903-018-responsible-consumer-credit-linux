"""客户授权与数据快照。

授权模型：客户按 scope（income / credit_report / debt_snapshot / marketing_use）
逐项授权，限定用途与有效期。所有读取敏感快照的动作都必须通过 require_scope，
并写入 snapshot_access_log，确保"在明确授权范围内读取、每次读取可审计"。
marketing_use 与授信/风控用途严格分离：风控结论不得回流入未经同意的促销。
"""

from .errors import ConsentRequired

SCOPE_INCOME = "income"
SCOPE_CREDIT_REPORT = "credit_report"
SCOPE_DEBT = "debt_snapshot"
SCOPE_MARKETING = "marketing_use"

RISK_SCOPES = (SCOPE_INCOME, SCOPE_CREDIT_REPORT, SCOPE_DEBT)
ALL_SCOPES = RISK_SCOPES + (SCOPE_MARKETING,)

# 每个用途只允许使用的 scope：防止把敏感画像挪作促销
PURPOSE_SCOPES = {
    "affordability_assessment": frozenset(RISK_SCOPES),
    "withdrawal_risk_check": frozenset(RISK_SCOPES),
    "hardship_review": frozenset(RISK_SCOPES),
    "collections": frozenset(),  # 催收不重新读取敏感画像，只使用决策时已固化数据
    "marketing": frozenset({SCOPE_MARKETING}),
}

SNAPSHOT_KIND_FOR_SCOPE = {
    SCOPE_INCOME: "income",
    SCOPE_CREDIT_REPORT: "credit_report",
    SCOPE_DEBT: "debt_snapshot",
}


def grant(storage, customer_id, scopes, purpose, now, expires_at=None):
    granted = []
    for scope in scopes:
        if scope not in ALL_SCOPES:
            raise ValueError(f"unknown_scope:{scope}")
        granted.append(
            storage.add_consent(customer_id, scope, purpose, now, expires_at, True)
        )
    return granted


def revoke(storage, customer_id, scope, now):
    storage.revoke_consent(customer_id, scope, now)


def require_scope(storage, customer_id, scope, purpose, actor, now_iso):
    """校验授权并留痕。失败抛 ConsentRequired；成功返回授权记录。"""
    allowed = PURPOSE_SCOPES.get(purpose)
    if allowed is None or scope not in allowed:
        raise ConsentRequired(
            f"scope {scope} 不允许用于 {purpose}",
            details={"scope": scope, "purpose": purpose},
        )
    consent = storage.active_consent(customer_id, scope, now_iso)
    if consent is None:
        raise ConsentRequired(
            f"缺少有效授权：{scope}", details={"scope": scope, "purpose": purpose}
        )
    return consent


def ingest_snapshot(storage, customer_id, kind, source, as_of, payload, taken_at):
    """登记一份外部数据快照（收入/征信/存量债务），不可变。"""
    return storage.add_snapshot(customer_id, kind, source, as_of, taken_at, payload)


def read_snapshot(storage, customer_id, kind, purpose, actor, now_iso):
    """在授权范围内读取最新快照，记录访问日志。返回 (payload_dict, snapshot_row)。"""
    scope = None
    for s, k in SNAPSHOT_KIND_FOR_SCOPE.items():
        if k == kind:
            scope = s
            break
    if scope is None:
        raise ConsentRequired(f"快照类型 {kind} 不允许直接读取")
    require_scope(storage, customer_id, scope, purpose, actor, now_iso)
    row = storage.latest_snapshot(customer_id, kind)
    if row is None:
        return None, None
    storage.log_snapshot_access(
        customer_id, row["id"], scope, purpose, actor, now_iso
    )
    import json

    return json.loads(row["payload"]), row


def read_risk_bundle(storage, customer_id, purpose, actor, now_iso):
    """读取一次可负担性/风险判断所需的三类快照，逐项校验授权并固化数据时点。"""
    bundle = {}
    provenance = {}
    for kind in ("income", "credit_report", "debt_snapshot"):
        payload, row = read_snapshot(storage, customer_id, kind, purpose, actor, now_iso)
        bundle[kind] = payload
        provenance[kind] = (
            {"snapshot_id": row["id"], "source": row["source"], "as_of": row["as_of"]}
            if row
            else None
        )
    return bundle, provenance

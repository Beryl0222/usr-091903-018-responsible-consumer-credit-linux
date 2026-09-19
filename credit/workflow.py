"""业务编排：申请、授信、提款、用途核验、放款、还款、困难协商、人工复核。

风控结论优先原则体现在：
- 授信/提款只接受规则引擎的结论，服务层不接受"营销目标"参数调整阈值或否决触发器；
- 触发器命中即冻结未放款部分并开人工案件，只有 risk_officer 凭书面理由可解除；
- 营销角色与风控数据物理隔离（见 consent.PURPOSE_SCOPES 与 api 角色控制）。
"""

import json
import uuid

from . import consent as consent_service
from . import ledger
from .collections import evaluate_contact
from .errors import (
    Conflict,
    InsufficientAvailable,
    NotFound,
    RiskHeld,
    ValidationFailed,
)
from .rules import RULE_VERSION, Rule
from .util import cents_to_yuan, yuan_to_cents

PRODUCTS = {
    "consumer_installment": {
        "annual_rate": "0.072",
        "default_term_months": 12,
        "evidence_required_before_disbursement": False,
    },
}


# ---------------- 申请与授信 ----------------

def apply_for_credit(storage, clock, customer_id, requested_yuan, *,
                     product="consumer_installment", term_months=None,
                     annual_rate=None, actor="system", idem=None):
    """读取授权快照并做可负担性评估，固化规则版本与数据时点。"""
    if product not in PRODUCTS:
        raise ValidationFailed(f"未知产品：{product}")
    product_cfg = PRODUCTS[product]
    annual_rate = annual_rate or product_cfg["annual_rate"]
    term_months = term_months or product_cfg["default_term_months"]
    requested_cents = yuan_to_cents(requested_yuan)
    if requested_cents <= 0:
        raise ValidationFailed("申请金额必须为正")

    now = clock.now().isoformat()

    application_id = storage.create_application(customer_id, requested_cents, product, now)
    bundle, provenance = consent_service.read_risk_bundle(
        storage, customer_id, "affordability_assessment", actor, now
    )
    for kind, info in provenance.items():
        if info is None:
            raise ValidationFailed(
                f"缺少 {kind} 快照，无法完成可负担性评估",
                details={"missing_snapshot": kind},
            )

    rule = Rule()
    result = rule.assess_affordability(bundle, requested_cents, term_months, annual_rate)
    result["inputs"] = _freeze_inputs(bundle, result, provenance)
    result["product"] = product
    result["annual_rate"] = annual_rate
    result["term_months"] = term_months

    storage.save_assessment(application_id, customer_id, result, now)
    if result["decision"] == "rejected":
        storage.set_application_status(application_id, "rejected", now)
    else:
        storage.set_application_status(application_id, "approved", now)
    return {"application_id": application_id, "assessment": _public_assessment(result)}


def offer_line(storage, clock, application_id, *, actor="system"):
    """对获批申请建立循环额度，额度上限来自规则结论而非申请金额。"""
    app = storage.get_application(application_id)
    if app is None:
        raise NotFound("申请不存在")
    assessment_row = storage.get_assessment_by_application(application_id)
    if assessment_row["decision"] != "approved":
        raise Conflict("仅获批申请可授信", details={"decision": assessment_row["decision"]})
    existing = storage.get_line(app["customer_id"])
    if existing is not None:
        raise Conflict("客户已有有效额度", details={"line_id": existing["id"]})

    now = clock.now().isoformat()
    # 额度按可负担性上限核定（非按本次申请金额）
    limit = assessment_row["approved_limit_cents"]

    def work(cur):
        line_id = storage.create_line(app["customer_id"], limit, now)
        storage.add_line_change(
            line_id, "grant", None, limit, "INITIAL_AFFORDABILITY",
            f"按规则 {assessment_row['rule_version']} 的可负担性上限授信",
            "assessment", application_id, now,
        )
        return line_id

    line_id = storage.transaction(work)
    return storage.get_line(app["customer_id"])


# ---------------- 提款 ----------------

def request_withdrawal(storage, clock, customer_id, amount_yuan, *,
                       merchant=None, usage_purpose=None, idempotency_key=None,
                       auto_disburse=True, actor="system"):
    """占用额度并做提款时风险复查。并发安全：占用为条件 UPDATE 原子操作。"""
    amount_cents = yuan_to_cents(amount_yuan)
    if amount_cents <= 0:
        raise ValidationFailed("提款金额必须为正")
    now = clock.now().isoformat()

    if idempotency_key:
        prior = storage.find_withdrawal_by_idem(customer_id, idempotency_key)
        if prior is not None:
            return _withdrawal_result(prior, idempotent_replay=True)

    line = storage.get_line(customer_id)
    if line is None:
        raise NotFound("客户尚无授信额度")
    if line["status"] != "active":
        raise Conflict(f"额度状态 {line['status']}，不可提款", details={"line_status": line["status"]})

    # 先做提款时风险复查所需的授权读取：授权/快照缺失时在占用前失败，不留垃圾占用
    bundle, provenance = consent_service.read_risk_bundle(
        storage, customer_id, "withdrawal_risk_check", actor, now
    )
    assessment_row = _latest_approved_assessment(storage, customer_id)
    frozen_inputs = json.loads(assessment_row["inputs"])
    frozen_inputs["proposed_usage_purpose"] = usage_purpose

    # 原子占用 + 建单（同一事务），杜绝并发超额
    withdrawal_id = storage.transaction(
        lambda cur: _reserve_and_create(
            storage, cur, line, customer_id, amount_cents, merchant, usage_purpose, now,
            idempotency_key,
        )
    )

    withdrawal = storage.get_withdrawal(withdrawal_id)

    rule = Rule()
    triggers, details = rule.withdrawal_risk_check(bundle, frozen_inputs)
    details["snapshots"] = provenance

    if triggers:
        return _hold_withdrawal(
            storage, clock, withdrawal, triggers, details,
            reason_scope="withdrawal", actor=actor,
        )

    if auto_disburse:
        return _disburse(storage, clock, withdrawal, PRODUCTS[withdrawal_merchant_product(storage, withdrawal)])
    storage.set_withdrawal_status(withdrawal_id, "reserved", now)
    return _withdrawal_result(storage.get_withdrawal(withdrawal_id))


def _reserve_and_create(storage, cur, line, customer_id, amount_cents, merchant, purpose, now,
                        idempotency_key=None):
    cur.execute(
        "UPDATE credit_lines SET available_cents=available_cents-?, updated_at=? "
        "WHERE id=? AND status='active' AND available_cents>=?",
        (amount_cents, now, line["id"], amount_cents),
    )
    if cur.rowcount != 1:
        raise InsufficientAvailable(
            "可用额度不足或额度已冻结",
            details={"requested_cents": amount_cents, "available_cents": line["available_cents"]},
        )
    wid = uuid.uuid4().hex
    cur.execute(
        "INSERT INTO withdrawals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (wid, line["id"], customer_id, amount_cents, "CNY",
         merchant.get("id") if merchant else None,
         merchant.get("name") if merchant else None,
         purpose, "reserved", idempotency_key, now, now, None),
    )
    return wid


def withdrawal_merchant_product(storage, withdrawal):
    app = storage.query_one(
        "SELECT product FROM credit_applications WHERE customer_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (withdrawal["customer_id"],),
    )
    return app["product"] if app else "consumer_installment"


def _hold_withdrawal(storage, clock, withdrawal, triggers, details, *, reason_scope, actor):
    now = clock.now().isoformat()
    as_of = json.dumps(
        {k: (v.get("as_of") if v else None) for k, v in details.get("snapshots", {}).items()},
        ensure_ascii=False,
    )

    def work(cur):
        storage.set_withdrawal_status(withdrawal["id"], "held", now)
        storage.set_line_status(withdrawal["line_id"], "frozen", now)
        case_id = storage.create_manual_case(
            withdrawal["customer_id"], "withdrawal", withdrawal["id"],
            triggers[0], now,
        )
        for code in triggers:
            storage.add_hold_reason(withdrawal["id"], code, details, as_of, now, case_id)
        storage.add_line_change(
            withdrawal["line_id"], "freeze", None, None, "RISK_HOLD",
            f"提款风险触发器：{','.join(triggers)}", "withdrawal", withdrawal["id"], now,
        )
        return case_id

    case_id = storage.transaction(work)
    raise RiskHeld(
        f"提款被风险规则暂停：{','.join(triggers)}，未放款部分已冻结，转人工复核",
        details={
            "withdrawal_id": withdrawal["id"],
            "manual_case_id": case_id,
            "triggers": triggers,
            "check_detail": details,
            "disbursed": False,
        },
    )


def _disburse(storage, clock, withdrawal, product_cfg):
    loan_id = ledger.disburse(
        storage, withdrawal, product_cfg["annual_rate"], product_cfg["default_term_months"], clock
    )
    now = clock.now().isoformat()
    storage.set_withdrawal_status(withdrawal["id"], "disbursed", now, disbursed_at=now)
    row = storage.get_withdrawal(withdrawal["id"])
    loan = storage.get_loan(loan_id)
    return {
        "withdrawal": _withdrawal_result(row)["withdrawal"],
        "disbursed": True,
        "loan_id": loan_id,
        "cost_disclosure": ledger.total_cost_disclosure(loan),
    }


def disburse_reserved(storage, clock, withdrawal_id):
    """对凭证核验通过（或人工已采信）的预留提款放款。"""
    withdrawal = storage.get_withdrawal(withdrawal_id)
    if withdrawal is None:
        raise NotFound("提款不存在")
    if withdrawal["status"] != "reserved":
        raise Conflict(
            f"提款状态 {withdrawal['status']}，不可放款",
            details={"status": withdrawal["status"]},
        )
    docs = storage.usage_documents(withdrawal_id)
    has_verified = any(d["doc_status"] == "verified" for d in docs)
    accepted_case = storage.query_one(
        "SELECT * FROM manual_cases WHERE subject_type='usage_evidence' AND subject_id=? "
        "AND case_status='decided' AND decision='evidence_accepted' "
        "ORDER BY decided_at DESC LIMIT 1",
        (withdrawal_id,),
    )
    if docs and not has_verified and accepted_case is None:
        raise Conflict("用途凭证尚未核验通过", details={"documents": docs})
    return _disburse(
        storage, clock, withdrawal, PRODUCTS[withdrawal_merchant_product(storage, withdrawal)]
    )


# ---------------- 用途核验 ----------------

def submit_usage_document(storage, clock, withdrawal_id, document, *, actor="customer"):
    withdrawal = storage.get_withdrawal(withdrawal_id)
    if withdrawal is None:
        raise NotFound("提款不存在")
    now = clock.now().isoformat()

    rule = Rule()
    status, conflicts = rule.verify_usage_document(withdrawal, document)
    record = {
        "doc_type": document.get("doc_type"),
        "claimed_amount_cents": document.get("claimed_amount_cents"),
        "claimed_merchant": document.get("claimed_merchant"),
        "claimed_purpose": document.get("claimed_purpose"),
        "doc_status": status,
        "conflict_detail": conflicts,
        "reviewed_at": now,
    }
    doc_id = storage.add_usage_document(withdrawal_id, withdrawal["customer_id"], record, now)

    if status == "conflict":
        # 用途凭证冲突：暂停未放款部分（冻结剩余额度），已放款部分转人工追查
        case_id = storage.create_manual_case(
            withdrawal["customer_id"], "usage_evidence", withdrawal_id,
            conflicts[0]["code"], now,
        )
        line = storage.get_line(withdrawal["customer_id"])
        if line and line["status"] == "active":
            storage.set_line_status(line["id"], "frozen", now)
            storage.add_line_change(
                line["id"], "freeze", None, None, "USAGE_EVIDENCE_CONFLICT",
                f"用途凭证冲突：{','.join(c['code'] for c in conflicts)}",
                "usage_document", doc_id, now,
            )
        storage.add_hold_reason(
            withdrawal_id, "USAGE_EVIDENCE_CONFLICT", {"conflicts": conflicts},
            None, now, case_id,
        )
    return {"document_id": doc_id, "status": status, "conflicts": conflicts}


# ---------------- 人工复核决定 ----------------

def decide_manual_case(storage, clock, case_id, decision, *, decided_by, rationale,
                       plan=None, reduced_amount_cents=None):
    """人工对风险暂停/用途冲突/困难协商作出决定。必须给出理由，全程留痕。

    decision 取值：
      withdrawal 案件: disburse / cancel / reduce_and_disburse
      usage_evidence  : evidence_accepted / evidence_rejected
      hardship (loan) : approve（配合 plan）/ reject
    """
    case = storage.get_manual_case(case_id)
    if case is None:
        raise NotFound("人工案件不存在")
    if case["case_status"] != "open":
        raise Conflict("案件已作出决定", details={"status": case["case_status"]})
    if not rationale or not rationale.strip():
        raise ValidationFailed("人工决定必须填写理由（risk_decision_rationale_required）")
    now = clock.now().isoformat()

    subject_type = case["subject_type"]
    result = None

    if subject_type == "withdrawal":
        result = _decide_withdrawal_case(
            storage, clock, case, decision, rationale, decided_by, reduced_amount_cents, now
        )
    elif subject_type == "usage_evidence":
        result = _decide_evidence_case(storage, clock, case, decision, rationale, decided_by, now)
    elif subject_type == "loan":
        result = _decide_hardship_case(
            storage, clock, case, decision, rationale, decided_by, plan, now
        )
    elif subject_type == "line_review":
        result = _decide_line_review(storage, case, decision, rationale, decided_by, now)
    else:
        raise ValidationFailed(f"未知案件类型：{subject_type}")

    storage.decide_manual_case(
        case_id, decided_by, decision, result or {}, rationale, now
    )
    return {"case_id": case_id, "decision": decision, "result": result}


def _resume_line(storage, customer_id, now):
    line = storage.get_line(customer_id)
    if line and line["status"] == "frozen":
        storage.set_line_status(line["id"], "active", now)


def _resume_line_if_no_open_risk_case(storage, customer_id, now, exclude_case_id=None):
    """困难/提款案件结案后恢复额度，但不得覆盖其他风险触发器造成的冻结。"""
    sql = ("SELECT 1 FROM manual_cases WHERE customer_id=? AND case_status='open' "
           "AND subject_type IN ('withdrawal','line_review','usage_evidence')")
    params = [customer_id]
    if exclude_case_id:
        sql += " AND id<>?"
        params.append(exclude_case_id)
    sql += " LIMIT 1"
    open_risk = storage.query_one(sql, params)
    if open_risk is None:
        _resume_line(storage, customer_id, now)


def _decide_withdrawal_case(storage, clock, case, decision, rationale, decided_by,
                            reduced_amount_cents, now):
    withdrawal = storage.get_withdrawal(case["subject_id"])
    line = storage.get_line(withdrawal["customer_id"])

    if decision == "cancel":
        if withdrawal["status"] == "held":
            storage.release_available(line["id"], withdrawal["amount_cents"])
        storage.set_withdrawal_status(withdrawal["id"], "cancelled", now)
        storage.resolve_hold_reasons(withdrawal["id"], case["id"])
        _resume_line_if_no_open_risk_case(storage, withdrawal["customer_id"], now, case["id"])
        return {"withdrawal_status": "cancelled", "released_cents": withdrawal["amount_cents"]}

    if decision == "disburse":
        if withdrawal["status"] != "held":
            raise Conflict("仅 held 状态的提款可决定放款")
        storage.set_withdrawal_status(withdrawal["id"], "reserved", now)
        storage.resolve_hold_reasons(withdrawal["id"], case["id"])
        _resume_line_if_no_open_risk_case(storage, withdrawal["customer_id"], now, case["id"])
        disbursed = _disburse(
            storage, clock, storage.get_withdrawal(withdrawal["id"]),
            PRODUCTS[withdrawal_merchant_product(storage, withdrawal)],
        )
        return {"withdrawal_status": "disbursed", "loan_id": disbursed["loan_id"]}

    if decision == "reduce_and_disburse":
        if not reduced_amount_cents or reduced_amount_cents <= 0 \
                or reduced_amount_cents > withdrawal["amount_cents"]:
            raise ValidationFailed("降额金额必须为正且不超过原提款金额")
        if withdrawal["status"] != "held":
            raise Conflict("仅 held 状态的提款可降额放款")
        storage.release_available(line["id"], withdrawal["amount_cents"] - reduced_amount_cents)
        storage.add_line_change(
            line["id"], "release", None, None,
            "MANUAL_RISK_REDUCTION",
            f"人工降额后放款，释放差额 {withdrawal['amount_cents'] - reduced_amount_cents} 分：{rationale}",
            "manual_case", case["id"], now,
        )
        # 以降额后的金额重建提款单并放款
        storage.execute(
            "UPDATE withdrawals SET amount_cents=?, status='reserved' WHERE id=?",
            (reduced_amount_cents, withdrawal["id"]),
        )
        storage.resolve_hold_reasons(withdrawal["id"], case["id"])
        _resume_line_if_no_open_risk_case(storage, withdrawal["customer_id"], now, case["id"])
        disbursed = _disburse(
            storage, clock, storage.get_withdrawal(withdrawal["id"]),
            PRODUCTS[withdrawal_merchant_product(storage, withdrawal)],
        )
        return {"withdrawal_status": "disbursed", "loan_id": disbursed["loan_id"],
                "disbursed_cents": reduced_amount_cents}

    raise ValidationFailed(f"提款案件不支持的决定：{decision}")


def _decide_evidence_case(storage, clock, case, decision, rationale, decided_by, now):
    withdrawal = storage.get_withdrawal(case["subject_id"])
    line = storage.get_line(withdrawal["customer_id"])
    if decision == "evidence_accepted":
        _resume_line_if_no_open_risk_case(storage, withdrawal["customer_id"], now, case["id"])
        storage.resolve_hold_reasons(withdrawal["id"], case["id"])
        return {"line_status": "active"}
    if decision == "evidence_rejected":
        # 用途不实：保持冻结并降额至已用水平（available=0），等待进一步追偿调查
        if line:
            old = line["total_limit_cents"]
            used = old - line["available_cents"]
            storage.adjust_limit(line["id"], used, now)
            storage.add_line_change(
                line["id"], "reduce", old, used, "USAGE_FRAUD_LIMIT_REDUCTION",
                f"用途凭证不实，额度降至已用水平：{rationale}",
                "manual_case", case["id"], now,
            )
        return {"line_status": line["status"] if line else None, "reduced": True}
    raise ValidationFailed(f"用途案件不支持的决定：{decision}")


def _decide_hardship_case(storage, clock, case, decision, rationale, decided_by, plan, now):
    loan = storage.get_loan(case["subject_id"])
    arrangement = storage.active_arrangement(loan["id"])
    if decision == "reject":
        if arrangement:
            storage.set_arrangement_status(
                arrangement["id"], "rejected", now, decided_by, {"rationale": rationale}
            )
        _resume_line_if_no_open_risk_case(storage, loan["customer_id"], now, case["id"])
        return {"arrangement_status": "rejected"}
    if decision != "approve" or not plan or plan.get("type") not in (
        "extension", "restructure", "forbearance"
    ):
        raise ValidationFailed("批准困难协商需提供 extension/restructure/forbearance 方案")
    result = ledger.restructure(
        storage, loan["id"], plan, clock, decided_by=decided_by, case_id=case["id"]
    )
    if arrangement:
        storage.set_arrangement_status(
            arrangement["id"], "active", now, decided_by, {"plan": plan, "rationale": rationale}
        )
    _resume_line_if_no_open_risk_case(storage, loan["customer_id"], now, case["id"])
    return result


def _decide_line_review(storage, case, decision, rationale, decided_by, now):
    line = storage.get_line(case["customer_id"])
    if decision == "keep_frozen":
        return {"line_status": "frozen"}
    if decision == "resume":
        _resume_line(storage, case["customer_id"], now)
        return {"line_status": "active"}
    if decision == "reduce":
        raise ValidationFailed("额度复查降额请走 plan 金额参数", details={"hint": "use adjust_line"})
    raise ValidationFailed(f"额度复查案件不支持的决定：{decision}")


# ---------------- 困难协商 ----------------

def request_hardship(storage, clock, loan_id, plan_type, *, reason, actor="customer", plan=None):
    loan = storage.get_loan(loan_id)
    if loan is None:
        raise NotFound("贷款不存在")
    if storage.active_arrangement(loan_id) is not None:
        raise Conflict("已有进行中的困难安排")
    now = clock.now().isoformat()
    arrangement_id = storage.add_arrangement(
        loan_id, loan["customer_id"], plan_type, "requested", now,
        {"reason": reason, "requested_plan": plan or {}},
    )
    case_id = storage.create_manual_case(
        loan["customer_id"], "loan", loan_id, "HARDSHIP_REQUEST", now
    )
    # 协商未决期间暂停未放款部分，待人工决定后恢复或重组
    line = storage.get_line(loan["customer_id"])
    if line and line["status"] == "active":
        storage.set_line_status(line["id"], "frozen", now)
        storage.add_line_change(
            line["id"], "freeze", None, None, "HARDSHIP_NEGOTIATION",
            f"客户申请困难协商（{plan_type}），未放款部分暂停", "arrangement",
            arrangement_id, now,
        )
    return {"arrangement_id": arrangement_id, "manual_case_id": case_id,
            "collections_paused": True}


# ---------------- 主动风险复查（收入/负债变化监控） ----------------

def review_line(storage, clock, customer_id, *, actor="risk_monitor"):
    """用最新快照对存量客户复查；命中触发器则冻结未放款部分并开案。"""
    line = storage.get_line(customer_id)
    if line is None:
        raise NotFound("客户尚无授信额度")
    now = clock.now().isoformat()
    bundle, provenance = consent_service.read_risk_bundle(
        storage, customer_id, "withdrawal_risk_check", actor, now
    )
    assessment_row = _latest_approved_assessment(storage, customer_id)
    frozen_inputs = json.loads(assessment_row["inputs"])
    triggers, details = Rule().withdrawal_risk_check(bundle, frozen_inputs)
    details["snapshots"] = provenance
    if not triggers:
        return {"line_status": line["status"], "triggers": []}
    if line["status"] == "active":
        storage.set_line_status(line["id"], "frozen", now)
    case_id = storage.create_manual_case(customer_id, "line_review", line["id"], triggers[0], now)
    storage.add_line_change(
        line["id"], "freeze", line["total_limit_cents"], line["total_limit_cents"],
        "PERIODIC_RISK_REVIEW", f"监控复查命中：{','.join(triggers)}",
        "risk_monitor", case_id, now,
    )
    return {"line_status": "frozen", "triggers": triggers, "manual_case_id": case_id,
            "check_detail": details}


def adjust_line(storage, clock, customer_id, new_limit_cents, reason_code, reason_detail,
                *, actor, ref_id=None):
    """人工降额（不可用营销理由）；调升需新的可负担性评估，此接口拒绝调升。"""
    line = storage.get_line(customer_id)
    if line is None:
        raise NotFound("额度不存在")
    if new_limit_cents > line["total_limit_cents"]:
        raise Conflict("调升额度必须重新进行可负担性评估，不允许直接调升")
    if not reason_detail:
        raise ValidationFailed("额度调整必须记录原因")
    now = clock.now().isoformat()
    old_limit, new_available = storage.adjust_limit(line["id"], new_limit_cents, now)
    storage.add_line_change(
        line["id"], "reduce", old_limit, new_limit_cents, reason_code, reason_detail,
        actor, ref_id, now,
    )
    return storage.get_line(line["id"])


# ---------------- 还款 / 退款 / 催收 ----------------

def repay(storage, clock, loan_id, amount_yuan, **kwargs):
    return ledger.repay(storage, loan_id, yuan_to_cents(amount_yuan), clock, **kwargs)


def merchant_refund(storage, clock, withdrawal_id, amount_yuan, *, route, ref_id=None):
    return ledger.merchant_reversal(
        storage, withdrawal_id, yuan_to_cents(amount_yuan), clock,
        kind="merchant_refund", route=route, ref_id=ref_id,
    )


def installment_cancel(storage, clock, withdrawal_id, *, route, amount_yuan=None, ref_id=None):
    withdrawal = storage.get_withdrawal(withdrawal_id)
    if withdrawal is None:
        raise NotFound("提款不存在")
    amount_cents = withdrawal["amount_cents"] if amount_yuan is None else yuan_to_cents(amount_yuan)
    return ledger.merchant_reversal(
        storage, withdrawal_id, amount_cents, clock,
        kind="installment_cancel", route=route, ref_id=ref_id,
    )


def collection_contact(storage, clock, loan_id, channel, *, actor):
    return evaluate_contact(storage, loan_id, channel, clock.now(), actor)


# ---------------- 客户视图 / 合规复现 / 营销隔离 ----------------

def customer_line_view(storage, customer_id):
    line = storage.get_line(customer_id)
    if line is None:
        raise NotFound("额度不存在")
    changes = [
        {
            "change_type": c["change_type"],
            "old_limit_yuan": cents_to_yuan(c["old_limit_cents"]) if c["old_limit_cents"] is not None else None,
            "new_limit_yuan": cents_to_yuan(c["new_limit_cents"]) if c["new_limit_cents"] is not None else None,
            "reason_code": c["reason_code"],
            "reason_detail": c["reason_detail"],
            "created_at": c["created_at"],
        }
        for c in storage.list_line_changes(line["id"])
    ]
    return {
        "line_id": line["id"],
        "total_limit_yuan": cents_to_yuan(line["total_limit_cents"]),
        "available_yuan": cents_to_yuan(line["available_cents"]),
        "used_yuan": cents_to_yuan(line["total_limit_cents"] - line["available_cents"]),
        "status": line["status"],
        "change_history": changes,
    }


def loan_view(storage, loan_id):
    return ledger.loan_summary(storage, loan_id)


def replay_assessment(storage, application_id):
    """用固化的输入与规则版本重新计算，验证决策可复现；并返回数据时点出处。"""
    row = storage.get_assessment_by_application(application_id)
    if row is None:
        raise NotFound("评估记录不存在")
    stored = {
        "rule_version": row["rule_version"],
        "decision": row["decision"],
        "max_amount_cents": row["max_amount_cents"],
        "reasons": json.loads(row["reasons"]),
        "inputs": json.loads(row["inputs"]),
        "created_at": row["created_at"],
    }
    if row["rule_version"] != RULE_VERSION:
        stored["reproducible"] = False
        stored["note"] = "规则版本已升级，历史结论按固化版本归档，不重算覆盖"
        return stored

    inputs = stored["inputs"]
    bundle = {
        "income": inputs["income"],
        "credit_report": inputs["credit_report"],
        "debt_snapshot": inputs["debt_snapshot"],
    }
    app = storage.get_application(application_id)
    recomputed = Rule().assess_affordability(
        bundle, app["requested_amount_cents"],
        inputs["metrics"]["term_months"], _annual_rate_of(storage, application_id, app),
    )
    stored["recomputed"] = {
        "decision": recomputed["decision"],
        "max_amount_cents": recomputed["max_amount_cents"],
        "reasons": recomputed["reasons"],
    }
    stored["reproducible"] = (
        recomputed["decision"] == stored["decision"]
        and recomputed["max_amount_cents"] == stored["max_amount_cents"]
    )
    stored["data_as_of"] = {
        k: (v.get("as_of") if isinstance(v, dict) else None)
        for k, v in bundle.items()
    }
    return stored


def _annual_rate_of(storage, application_id, app):
    # 评估时的利率随申请产品固化
    return PRODUCTS.get(app["product"], PRODUCTS["consumer_installment"])["annual_rate"]


def marketing_eligibility(storage, customer_id, now_iso):
    """营销准入只认 marketing_use 授权；严禁读取风控画像。"""
    consent_row = storage.active_consent(customer_id, "marketing_use", now_iso)
    return {
        "customer_id": customer_id,
        "marketing_consent": consent_row is not None,
        "eligible": consent_row is not None,
        "risk_data_used": False,
    }


# ---------------- 辅助 ----------------

def _latest_approved_assessment(storage, customer_id):
    row = storage.query_one(
        """
        SELECT a.* FROM affordability_assessments a
        JOIN credit_applications p ON p.id = a.application_id
        WHERE a.customer_id=? AND a.decision='approved'
        ORDER BY a.created_at DESC LIMIT 1
        """,
        (customer_id,),
    )
    if row is None:
        raise NotFound("缺少已批准的可负担性评估，无法进行提款复查")
    return row


def _freeze_inputs(bundle, result, provenance):
    income = bundle.get("income") or {}
    credit = bundle.get("credit_report") or {}
    debt = bundle.get("debt_snapshot") or {}
    return {
        "income": {
            "monthly_income_cents": income.get("monthly_income_cents"),
            "status": income.get("status"),
            "as_of": provenance["income"]["as_of"],
            "snapshot_id": provenance["income"]["snapshot_id"],
        },
        "credit_report": {
            "in_grace_period": credit.get("in_grace_period"),
            "has_overdue": credit.get("has_overdue"),
            "max_overdue_days": credit.get("max_overdue_days"),
            "as_of": provenance["credit_report"]["as_of"],
            "snapshot_id": provenance["credit_report"]["snapshot_id"],
        },
        "debt_snapshot": {
            "monthly_obligation_cents": debt.get("monthly_obligation_cents"),
            "institution_count": debt.get("institution_count"),
            "total_outstanding_cents": debt.get("total_outstanding_cents"),
            "new_short_term_debt_cents_since_assessment": debt.get(
                "new_short_term_debt_cents_since_assessment", 0
            ),
            "as_of": provenance["debt_snapshot"]["as_of"],
            "snapshot_id": provenance["debt_snapshot"]["snapshot_id"],
        },
        "metrics": result["metrics"],
        "flags": result["flags"],
        "proposed_usage_purpose": None,
    }


def _public_assessment(result):
    return {
        "rule_version": result["rule_version"],
        "decision": result["decision"],
        "max_amount_yuan": cents_to_yuan(result["max_amount_cents"]),
        "approved_limit_yuan": cents_to_yuan(result["approved_limit_cents"]),
        "monthly_payment_yuan": cents_to_yuan(result["payment_cents"]),
        "reasons": result["reasons"],
        "metrics": {
            k: (cents_to_yuan(v) if k.endswith("_cents") and v is not None else v)
            for k, v in result["metrics"].items()
        },
        "flags": result["flags"],
        "annual_rate": result["annual_rate"],
        "term_months": result["term_months"],
    }


def _withdrawal_result(row, idempotent_replay=False):
    return {
        "withdrawal": {
            "id": row["id"],
            "amount_yuan": cents_to_yuan(row["amount_cents"]),
            "status": row["status"],
            "merchant_id": row["merchant_id"],
            "usage_purpose": row["usage_purpose"],
            "created_at": row["created_at"],
            "disbursed_at": row["disbursed_at"],
        },
        "disbursed": row["status"] == "disbursed",
        "idempotent_replay": idempotent_replay,
    }

"""合规复现与数据访问监督。

- reproduce_assessment：用一次评估落库时的规则版本 + 当时的不可变快照，
  重新跑纯计算，比对结论；并附申请、快照内容、授权、后续人工决定、
  额度变化、信号、催收边界的完整时间线。
- 复现不重新读数据、不改变任何状态，因此可被合规反复执行。
"""

from credit import clock
from credit.audit import audit
from credit.errors import forbidden
from credit.rules import AffordabilityEngine
from credit.store import Store

COMPLIANCE_ROLES = ("compliance", "admin")


class ComplianceService:
    def __init__(self, store: Store, consents=None):
        self.store = store
        self.consents = consents

    def reproduce_assessment(self, assessment_id: str, actor: str, role: str) -> dict:
        if role not in COMPLIANCE_ROLES:
            raise forbidden("仅合规岗位可以复现决策", {"actor_role": role})
        asm = self.store.require("assessments", assessment_id)
        inputs = Store.loads(asm["inputs"])
        version = inputs["rule_set_version"]
        app = self.store.row_to_dict(self.store.require("applications", asm["application_id"]))

        snapshot_rows = {}
        snapshots = {}
        for kind, ref in inputs["snapshots"].items():
            row = self.store.require("snapshot_reads", ref["id"])
            snapshot_rows[kind] = self.store.row_to_dict(row)
            snapshots[kind] = Store.loads(row["payload"])

        recomputed = AffordabilityEngine.compute(
            app, snapshots["income"], snapshots["debt"], snapshots["credit_report"],
            version,
        )
        stored = {
            "decision": asm["decision"],
            "approved_limit_cents": asm["approved_limit_cents"],
            "reasons": Store.loads(asm["triggered_reasons"]),
            "calculation": Store.loads(asm["calculation"]),
        }
        matches = (recomputed["decision"] == stored["decision"]
                   and recomputed["approved_limit_cents"] == stored["approved_limit_cents"]
                   and recomputed["reasons"] == stored["reasons"])

        timeline = self._timeline(asm, app, snapshot_rows, inputs)
        audit(self.store, actor, role, "reproduce_assessment",
              "assessment", assessment_id,
              {"matches": matches, "rule_set_version": version})

        return {
            "assessment_id": assessment_id,
            "rule_set_version": version,
            "matches": matches,
            "stored_decision": stored,
            "recomputed": {
                "decision": recomputed["decision"],
                "approved_limit_cents": recomputed["approved_limit_cents"],
                "reasons": recomputed["reasons"],
                "calculation": recomputed["calculation"],
            },
            "application": app,
            "inputs_detail": inputs,
            "snapshots_used": snapshot_rows,
            "timeline": timeline,
            "reproduced_at": clock.now_iso(),
        }

    def collection_boundary_report(self, customer_id: str, actor: str, role: str) -> dict:
        """催收联系边界报告：困难协商状态 + 全部联系记录及闸门判定。"""
        if role not in COMPLIANCE_ROLES:
            raise forbidden("仅合规岗位可以查看催收边界报告")
        rows = self.store.query(
            "SELECT * FROM collection_contacts WHERE customer_id=? ORDER BY at", (customer_id,))
        contacts = [self.store.row_to_dict(r) for r in rows]
        hardship_rows = self.store.query(
            "SELECT * FROM hardship_requests WHERE customer_id=? ORDER BY created_at", (customer_id,))
        audit(self.store, actor, role, "collection_boundary_report",
              "customer", customer_id, {"contact_count": len(contacts)})
        return {
            "customer_id": customer_id,
            "rules": {
                "contact_window": "08:00-21:00 客户当地时间",
                "max_per_day": 1,
                "max_per_7_days": 3,
                "stop_during_hardship": True,
                "only_for_overdue_loans": True,
            },
            "hardship_requests": [self.store.row_to_dict(r) for r in hardship_rows],
            "contacts": contacts,
        }

    def consent_usage_report(self, customer_id: str, actor: str, role: str) -> dict:
        """授权使用台账：每次数据读取的授权依据、时点、用途。"""
        if role not in COMPLIANCE_ROLES:
            raise forbidden("仅合规岗位可以查看授权使用台账")
        rows = self.store.query(
            "SELECT * FROM snapshot_reads WHERE customer_id=? ORDER BY read_at", (customer_id,))
        reads = []
        for r in rows:
            item = self.store.row_to_dict(r)
            item["payload"] = Store.loads(item["payload"])
            reads.append(item)
        consents = self.store.query(
            "SELECT * FROM consents WHERE customer_id=? ORDER BY granted_at", (customer_id,))
        audit(self.store, actor, role, "consent_usage_report", "customer", customer_id,
              {"read_count": len(reads)})
        return {
            "customer_id": customer_id,
            "consents": [self.store.row_to_dict(r) for r in consents],
            "snapshot_reads": reads,
        }

    # --- 时间线 ---------------------------------------------------------

    def _timeline(self, asm, app, snapshot_rows, inputs) -> list[dict]:
        events = []
        for kind, row in snapshot_rows.items():
            events.append({
                "at": row["read_at"], "type": "data_read", "kind": kind,
                "detail": {"source": row["source"], "as_of": row["as_of"],
                           "consent_id": row["consent_id"], "purpose": row["purpose"]},
            })
        events.append({"at": asm["created_at"], "type": "assessment",
                       "detail": {"decision": asm["decision"],
                                  "rule_set_version": asm["rule_set_version"]}})
        account = self.store.query_one(
            "SELECT * FROM credit_accounts WHERE application_id=?", (app["id"],))
        if account:
            for r in self.store.query(
                    "SELECT * FROM limit_change_events WHERE account_id=? ORDER BY created_at",
                    (account["id"],)):
                events.append({"at": r["created_at"], "type": "limit_change",
                               "detail": {"old": r["old_limit_cents"], "new": r["new_limit_cents"],
                                          "reason_code": r["reason_code"], "actor": r["actor"]}})
        for r in self.store.query(
                "SELECT * FROM signal_events WHERE customer_id=? ORDER BY created_at",
                (asm["customer_id"],)):
            events.append({"at": r["created_at"], "type": "risk_signal",
                           "detail": {"code": r["code"], "case_id": r["review_case_id"]}})
        for r in self.store.query(
                "SELECT * FROM review_cases WHERE customer_id=? ORDER BY created_at",
                (asm["customer_id"],)):
            if r["decided_at"]:
                events.append({"at": r["decided_at"], "type": "manual_decision",
                               "detail": {"decision": r["decision"], "reviewer": r["reviewer"],
                                          "reason": r["reason_detail"]}})
        for r in self.store.query(
                "SELECT * FROM collection_contacts WHERE customer_id=? ORDER BY at",
                (asm["customer_id"],)):
            events.append({"at": r["at"], "type": "collection_contact",
                           "detail": {"channel": r["channel"], "actor": r["actor"],
                                      "loan_id": r["loan_id"]}})
        return sorted(events, key=lambda e: e["at"])

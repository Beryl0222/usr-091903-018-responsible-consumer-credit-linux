"""贷后监控：在 ongoing_monitoring 授权下读取新快照，产生风险信号。

三类硬信号（题目要求）：
- income_dropped            收入骤降（新收入快照较基线下降达到阈值）
- suspected_rollover        疑似借新还旧 / 窗口内多头新增负债
- purpose_evidence_conflict 用途凭证冲突（在提款阶段产生，见 creditline）

信号触发后的动作（同一事务内完成）：
1. 账户置为 suspended，阻止一切新增提款；
2. 该客户所有未放款提款（reserved/evidence_pending/ready）置 suspended，
   预留金额保留占用，等人工决定恢复或驳回——即"暂停未放款部分"；
3. 开立 open 状态的人工复核案件，营销/系统均无权关闭它。
"""

from decimal import Decimal

from credit import clock
from credit.rules import RULE_SETS
from credit.store import Store, new_id

PURPOSE = "ongoing_monitoring"


class MonitoringService:
    def __init__(self, store: Store, consents):
        self.store = store
        self.consents = consents

    def ingest_income(self, customer_id: str, source: str, as_of: str, payload: dict) -> dict:
        """读取一份新收入快照（需要 ongoing_monitoring 授权），并判断收入骤降/中断。"""
        snapshot = self.consents.record_read(
            customer_id, "income", PURPOSE, source, as_of, payload)
        with self.store.customer_lock(customer_id), self.store.transaction():
            reasons = []
            months = payload.get("months") or []
            if months:
                drop = self._drop_ratio(months)
                threshold = Decimal(RULE_SETS["v1.0"]["params"]["income_drop_block_ratio"])
                if drop is not None and drop >= threshold:
                    reasons.append(("income_dropped", {"drop_ratio": str(drop),
                                                        "threshold": str(threshold)}))
            if payload.get("employment_status") in RULE_SETS["v1.0"]["params"]["income_interruption_statuses"]:
                reasons.append(("income_interrupted",
                                {"employment_status": payload.get("employment_status")}))
            for code, extra in reasons:
                self._raise(customer_id, code, {"snapshot_id": snapshot["id"], **extra})
        return self.store.row_to_dict(snapshot)

    def ingest_debt(self, customer_id: str, source: str, as_of: str, payload: dict) -> dict:
        """读取一份新存量债务快照，判断借新还旧/多头。"""
        snapshot = self.consents.record_read(
            customer_id, "debt", PURPOSE, source, as_of, payload)
        with self.store.customer_lock(customer_id), self.store.transaction():
            income_snap = self.store.latest_snapshot(customer_id, "income")
            income = 0
            if income_snap is not None:
                income = int(Store.loads(income_snap["payload"]).get("monthly_income_cents", 0))
            facilities = payload.get("facilities", [])
            window = int(RULE_SETS["v1.0"]["params"]["rollover_window_days"])
            new_facilities = [f for f in facilities
                              if int(f.get("opened_days_ago", 10_000)) <= window]
            new_payment = sum(int(f.get("monthly_payment_cents", 0)) for f in new_facilities)
            ratio = (Decimal(new_payment) / Decimal(income)) if income else Decimal("0")
            hit = (len(new_facilities) >= int(RULE_SETS["v1.0"]["params"]["rollover_new_facilities_block"])
                   or ratio >= Decimal(RULE_SETS["v1.0"]["params"]["rollover_new_debt_payment_ratio"]))
            if hit:
                self._raise(customer_id, "suspected_rollover", {
                    "snapshot_id": snapshot["id"],
                    "window_days": window,
                    "new_facilities": len(new_facilities),
                    "new_monthly_payment_cents": new_payment,
                    "new_payment_ratio": str(round(ratio, 4)),
                })
        return self.store.row_to_dict(snapshot)

    def _raise(self, customer_id: str, code: str, payload: dict) -> dict:
        now = clock.now_iso()
        account = self.store.query_one(
            "SELECT * FROM credit_accounts WHERE customer_id=? ORDER BY opened_at DESC LIMIT 1",
            (customer_id,))
        account_id = account["id"] if account else None
        signal = {
            "id": new_id("sig"), "customer_id": customer_id,
            "account_id": account_id, "drawdown_id": None, "loan_id": None,
            "code": code, "severity": "block",
            "payload": Store.dumps(payload), "review_case_id": None, "created_at": now,
        }
        self.store.insert("signal_events", signal)

        if account_id:
            self.store._conn.execute(
                "UPDATE credit_accounts SET status='suspended' WHERE id=? AND status='active'",
                (account_id,))
            pending = self.store.query(
                "SELECT id, hold_reasons FROM drawdowns WHERE account_id=? "
                "AND status IN ('reserved','evidence_pending','ready')", (account_id,))
            for row in pending:
                reasons = Store.loads(row["hold_reasons"], [])
                if code not in reasons:
                    reasons.append(code)
                self.store._conn.execute(
                    "UPDATE drawdowns SET status='suspended', hold_reasons=? WHERE id=?",
                    (Store.dumps(reasons), row["id"]))

        case = {
            "id": new_id("rev"), "customer_id": customer_id,
            "account_id": account_id, "drawdown_id": None, "loan_id": None,
            "application_id": None,
            "signal_ids": Store.dumps([signal["id"]]),
            "topic": "monitoring", "status": "open", "decision": None,
            "new_limit_cents": None, "new_term_months": None, "new_annual_rate": None,
            "reason_detail": None, "reviewer": None, "decided_at": None,
            "created_at": now,
        }
        self.store.insert("review_cases", case)
        self.store._conn.execute(
            "UPDATE signal_events SET review_case_id=? WHERE id=?", (case["id"], signal["id"]))
        return case

    @staticmethod
    def _drop_ratio(months: list[dict]):
        ordered = sorted(months, key=lambda m: m["month"])
        if len(ordered) < 2:
            return None
        latest = Decimal(ordered[-1]["income_cents"])
        baseline_months = ordered[-4:-1]
        if not baseline_months:
            return None
        baseline = sum(Decimal(m["income_cents"]) for m in baseline_months) / len(baseline_months)
        if baseline == 0:
            return None
        return (baseline - latest) / baseline

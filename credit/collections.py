"""催收联系边界。

强制规则（违规直接拒绝，不产生联系记录之外的外呼动作）：
- 只允许 08:00–21:00（客户当地时间，联系记录必须带 UTC 偏移量）；
- 每天最多 1 次、每 7 天最多 3 次（按自然日/滚动 7 天）；
- 困难协商生效期间一律停止催收；
- 只能对真实逾期贷款发起；未逾期贷款不得催收；
- 每次联系均留痕（渠道、时间、结果、经办人、备注），合规可复现联系边界。

本服务不存储也不使用敏感画像做联系人施压；仅依据台账逾期事实。
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

from credit import clock
from credit.errors import forbidden, validation_error
from credit.store import Store, new_id

CHANNELS = ("phone", "sms", "letter", "app_message")
START_HOUR = 8
END_HOUR = 21          # 21:00 之后禁止
MAX_PER_DAY = 1
MAX_PER_7_DAYS = 3


class CollectionService:
    def __init__(self, store: Store, hardship=None, timezone: str = "Asia/Shanghai"):
        self.store = store
        self.hardship = hardship
        self.timezone = timezone

    def can_contact(self, customer_id: str, loan_id: str | None = None,
                    at=None) -> dict:
        """返回 {allowed, reasons}；不写库。"""
        reasons = []
        local_dt = self._local(at)
        if local_dt.hour < START_HOUR or local_dt.hour >= END_HOUR:
            reasons.append("outside_contact_window")
        day_start = (local_dt - timedelta(hours=local_dt.hour, minutes=local_dt.minute,
                                          seconds=local_dt.second, microseconds=local_dt.microsecond))
        day_start_utc = day_start.astimezone(ZoneInfo("UTC")).isoformat()
        week_start_utc = (local_dt - timedelta(days=7)).astimezone(ZoneInfo("UTC")).isoformat()
        at_utc = local_dt.astimezone(ZoneInfo("UTC")).isoformat()

        params = [customer_id, day_start_utc, at_utc]
        day_count = self.store.query_one(
            "SELECT COUNT(*) c FROM collection_contacts WHERE customer_id=? AND at>=? AND at<=?",
            params)["c"]
        if day_count >= MAX_PER_DAY:
            reasons.append("daily_contact_limit")
        week_count = self.store.query_one(
            "SELECT COUNT(*) c FROM collection_contacts WHERE customer_id=? AND at>? AND at<=?",
            [customer_id, week_start_utc, at_utc])["c"]
        if week_count >= MAX_PER_7_DAYS:
            reasons.append("weekly_contact_limit")

        if self.hardship and self.hardship.active_for(customer_id, loan_id):
            reasons.append("hardship_negotiation_active")

        if loan_id:
            loan = self.store.require("loans", loan_id)
            if not self._is_overdue(loan, local_dt.date()):
                reasons.append("loan_not_overdue")
        return {"allowed": not reasons, "reasons": reasons,
                "checked_at": local_dt.isoformat()}

    def contact(self, customer_id: str, channel: str, actor: str,
                loan_id: str | None = None, at_iso: str | None = None,
                result: str | None = None, note: str | None = None) -> dict:
        if channel not in CHANNELS:
            raise validation_error(f"联系渠道必须是 {CHANNELS} 之一")
        local_dt = self._local(clock.parse(at_iso) if at_iso else clock.now())
        check = self.can_contact(customer_id, loan_id, local_dt)
        if not check["allowed"]:
            raise forbidden("当前不允许催收联系", {"reasons": check["reasons"]})

        row = {
            "id": new_id("col"), "customer_id": customer_id, "loan_id": loan_id,
            "account_id": None, "channel": channel,
            # 统一存 UTC，避免不同偏移字符串比较出错；展示时按时区转换
            "at": local_dt.astimezone(ZoneInfo("UTC")).isoformat(),
            "result": result, "actor": actor,
            "note": note, "created_at": clock.now_iso(),
        }
        if loan_id:
            row["account_id"] = self.store.get("loans", loan_id)["account_id"]
        with self.store.transaction():
            self.store.insert("collection_contacts", row)
        return self.store.row_to_dict(row)

    def history(self, customer_id: str) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM collection_contacts WHERE customer_id=? ORDER BY at DESC",
            (customer_id,))
        return [self.store.row_to_dict(r) for r in rows]

    # --- 内部 -----------------------------------------------------------

    def _local(self, dt):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo(self.timezone))

    @staticmethod
    def _is_overdue(loan, today) -> bool:
        schedule = Store.loads(loan["schedule"])
        for r in schedule:
            if r["status"] not in ("paid", "reversed") and r["due_date"] and r["due_date"] < today.isoformat():
                return True
        return False

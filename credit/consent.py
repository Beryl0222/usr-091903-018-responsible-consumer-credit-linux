"""客户授权管理。

边界：
- 风控数据授权（consents）与营销同意（marketing_grants）是两份独立意思表示，
  任何一个都不隐含另一个。
- scope 限定读什么：income_read / debt_read / credit_report_read。
- purpose 限定为什么读：credit_assessment / ongoing_monitoring / hardship_review。
- 每次真正读取外部数据都通过 record_read 留痕（授权、时点、用途）。
- 授权撤销立即生效；过期授权不可再读。
"""

from credit import clock
from credit.errors import consent_required, validation_error
from credit.store import Store, new_id

DATA_SCOPES = ("income_read", "debt_read", "credit_report_read")
PURPOSES = ("credit_assessment", "ongoing_monitoring", "hardship_review")

SCOPE_BY_KIND = {
    "income": "income_read",
    "debt": "debt_read",
    "credit_report": "credit_report_read",
}


class ConsentService:
    def __init__(self, store: Store):
        self.store = store

    def grant(self, customer_id: str, scopes: list[str], purposes: list[str],
              grant_ref: str, ttl_days: int = 180) -> dict:
        unknown = [s for s in scopes if s not in DATA_SCOPES]
        if unknown:
            raise validation_error(f"未知授权范围: {unknown}")
        bad_purposes = [p for p in purposes if p not in PURPOSES]
        if bad_purposes:
            raise validation_error(f"未知授权用途: {bad_purposes}")
        if not scopes:
            raise validation_error("至少需要一个授权范围")
        now = clock.now()
        from datetime import timedelta
        row = {
            "id": new_id("cst"),
            "customer_id": customer_id,
            "scopes": Store.dumps(scopes),
            "purposes": Store.dumps(purposes),
            "grant_ref": grant_ref,
            "granted_at": now.isoformat(),
            "expires_at": (now + timedelta(days=ttl_days)).isoformat(),
            "revoked_at": None,
        }
        with self.store.transaction():
            self.store.insert("consents", row)
        return row

    def revoke(self, consent_id: str) -> dict:
        consent = self.store.require("consents", consent_id)
        with self.store.transaction():
            self.store._conn.execute(
                "UPDATE consents SET revoked_at=? WHERE id=?",
                (clock.now_iso(), consent_id),
            )
        return self.store.require("consents", consent_id)

    def find_valid(self, customer_id: str, scope: str, purpose: str):
        """返回当前覆盖指定 scope+purpose 且有效的最新授权，否则 None。"""
        now = clock.now_iso()
        rows = self.store.query(
            "SELECT * FROM consents WHERE customer_id=? AND revoked_at IS NULL "
            "AND expires_at>? ORDER BY granted_at DESC",
            (customer_id, now),
        )
        for row in rows:
            scopes = Store.loads(row["scopes"])
            purposes = Store.loads(row["purposes"])
            if scope in scopes and purpose in purposes:
                return row
        return None

    def require_scope(self, customer_id: str, scope: str, purpose: str):
        consent = self.find_valid(customer_id, scope, purpose)
        if consent is None:
            raise consent_required(scope, {"required_scope": scope, "purpose": purpose})
        return consent

    def record_read(self, customer_id: str, kind: str, purpose: str,
                    source: str, as_of: str, payload: dict) -> dict:
        """校验授权并落一份不可变数据快照与读取记录。"""
        scope = SCOPE_BY_KIND.get(kind)
        if scope is None:
            raise validation_error(f"未知数据类型: {kind}")
        consent = self.require_scope(customer_id, scope, purpose)
        row = {
            "id": new_id("snap"),
            "customer_id": customer_id,
            "consent_id": consent["id"],
            "kind": kind,
            "source": source,
            "as_of": as_of,
            "purpose": purpose,
            "payload": Store.dumps(payload),
            "read_at": clock.now_iso(),
        }
        with self.store.transaction():
            self.store.insert("snapshot_reads", row)
        return row

    # --- 营销同意：独立意思表示 -----------------------------------------

    def set_marketing(self, customer_id: str, granted: bool,
                      channels: list[str] | None = None) -> dict:
        now = clock.now_iso()
        existing = self.store.query_one(
            "SELECT * FROM marketing_grants WHERE customer_id=? ORDER BY created_at DESC LIMIT 1",
            (customer_id,),
        )
        row = {
            "id": new_id("mkt"),
            "customer_id": customer_id,
            "granted": 1 if granted else 0,
            "channels": Store.dumps(channels or []),
            "granted_at": now if granted else None,
            "revoked_at": None if granted else now,
            "created_at": now,
        }
        with self.store.transaction():
            self.store.insert("marketing_grants", row)
        return row

    def marketing_allowed(self, customer_id: str, channel: str | None = None) -> tuple[bool, str]:
        row = self.store.query_one(
            "SELECT * FROM marketing_grants WHERE customer_id=? ORDER BY created_at DESC LIMIT 1",
            (customer_id,),
        )
        if row is None or not row["granted"]:
            return False, "客户未给予营销同意"
        channels = Store.loads(row["channels"], [])
        if channel and channels and channel not in channels:
            return False, f"营销同意未覆盖渠道: {channel}"
        return True, "ok"

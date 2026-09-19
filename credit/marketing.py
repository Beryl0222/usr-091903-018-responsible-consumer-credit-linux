"""营销闸门。

红线：
- 风控数据授权（income/debt/credit scope）不构成营销同意，二者独立；
- 没有明确营销同意，或客户已撤销，一律不得促销——无论营销目标如何；
- 促销环节禁止读取/使用可负担性画像与敏感快照（收入、征信、负债），
  即"敏感画像不得用于未经同意的促销"；
- 每次营销尝试（允许或拒绝）都写 marketing_actions，合规可查。
"""

from credit import clock
from credit.errors import forbidden
from credit.store import new_id

# 敏感画像特征前缀：任何风险/偿债相关标签都不得用于促销
SENSITIVE_TRAIT_PREFIXES = (
    "credit_", "income_", "debt_", "dti_", "risk_",
    "affordability", "repayment_", "overdue_",
)


class MarketingService:
    def __init__(self, store, consents):
        self.store = store
        self.consents = consents

    @staticmethod
    def _is_sensitive(trait: str) -> bool:
        return any(trait == p.rstrip("_") or trait.startswith(p)
                   for p in SENSITIVE_TRAIT_PREFIXES)

    def campaign(self, customer_id: str, channel: str, actor: str,
                 traits_used: list[str] | None = None) -> dict:
        """发起一次促销触达的准入判定；拒绝即抛 forbidden，并仍然留痕。"""
        traits_used = traits_used or []
        sensitive = [t for t in traits_used if self._is_sensitive(t)]
        allowed, reason = self.consents.marketing_allowed(customer_id, channel)

        if sensitive:
            allowed = False
            reason = f"促销不得使用敏感风险画像: {sensitive}"

        granted = bool(allowed) and not sensitive
        decision = {
            "id": new_id("mka"),
            "customer_id": customer_id,
            "channel": channel,
            "allowed": 1 if granted else 0,
            "reason": "ok" if granted else reason,
            "actor": actor,
            "at": clock.now_iso(),
        }
        with self.store.transaction():
            self.store.insert("marketing_actions", decision)
        if not granted:
            raise forbidden("促销被拦截",
                            {"reason": decision["reason"], "sensitive_traits": sensitive})
        return {"id": decision["id"], "customer_id": customer_id, "channel": channel,
                "allowed": True, "reason": "ok"}

    def action_log(self, customer_id: str) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM marketing_actions WHERE customer_id=? ORDER BY at DESC",
            (customer_id,))
        return [self.store.row_to_dict(r) for r in rows]

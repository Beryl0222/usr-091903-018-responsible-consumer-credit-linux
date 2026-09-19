"""审计日志：合规/管理动作留痕（谁、角色、动作、对象、理由）。"""

from credit import clock
from credit.store import new_id


def audit(store, actor: str, role: str, action: str,
          entity_type: str | None = None, entity_id: str | None = None,
          detail: dict | None = None) -> None:
    with store.transaction():
        store.insert("audit_log", {
            "id": new_id("aud"),
            "actor": actor,
            "role": role,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "detail": store.dumps(detail or {}),
            "created_at": clock.now_iso(),
        })


def list_audit(store, entity_type: str | None = None, entity_id: str | None = None,
               limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM audit_log WHERE 1=1"
    params = []
    if entity_type:
        sql += " AND entity_type=?"
        params.append(entity_type)
    if entity_id:
        sql += " AND entity_id=?"
        params.append(entity_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = store.query(sql, params)
    result = []
    for r in rows:
        item = store.row_to_dict(r)
        item["detail"] = store.loads(item["detail"])
        result.append(item)
    return result

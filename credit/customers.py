"""客户档案（最小化：只存服务必需的标识信息，敏感画像不入本系统）。"""

from credit import clock
from credit.errors import validation_error
from credit.store import Store


class CustomerService:
    def __init__(self, store: Store):
        self.store = store

    def create(self, customer_id: str, name: str) -> dict:
        if not customer_id or not name:
            raise validation_error("客户编号与姓名必填")
        if self.store.get("customers", customer_id) is not None:
            raise validation_error(f"客户已存在: {customer_id}")
        row = {
            "id": customer_id,
            "name": name,
            "created_at": clock.now_iso(),
        }
        with self.store.transaction():
            self.store.insert("customers", row)
        return row

    def get(self, customer_id: str) -> dict:
        return self.store.row_to_dict(self.store.require("customers", customer_id))

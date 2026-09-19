"""SQLite 存储层。

设计要点：
- 金钱一律 INTEGER 分；时间一律 ISO-8601 UTC 字符串；快照/规则输入不可变。
- 每个客户一把进程内锁，配合 BEGIN IMMEDIATE 事务，保证并发提款不突破有效额度。
- 快照一经写入不可修改（代码层不提供 update 接口），保证合规复现的输入稳定。
"""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager



def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- 风控数据授权：与营销同意严格分开
CREATE TABLE IF NOT EXISTS consents (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  scopes TEXT NOT NULL,           -- JSON 数组: income_read/debt_read/credit_report_read
  purposes TEXT NOT NULL,         -- JSON 数组: credit_assessment/ongoing_monitoring/hardship_review
  grant_ref TEXT NOT NULL,        -- 授权书/勾选记录编号
  granted_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT
);

-- 每次读取外部数据都留痕：读了什么、凭哪份授权、为什么读、数据时点
CREATE TABLE IF NOT EXISTS snapshot_reads (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  consent_id TEXT NOT NULL,
  kind TEXT NOT NULL,             -- income / debt / credit_report
  source TEXT NOT NULL,
  as_of TEXT NOT NULL,            -- 数据对应的业务时点（快照日期）
  purpose TEXT NOT NULL,
  payload TEXT NOT NULL,          -- JSON 原始内容，不可变
  read_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  requested_cents INTEGER NOT NULL,
  term_months INTEGER NOT NULL,
  declared_purpose TEXT NOT NULL,
  merchant_category TEXT,
  annual_rate TEXT NOT NULL,
  status TEXT NOT NULL,           -- submitted/approved/rejected/withdrawn
  created_at TEXT NOT NULL
);

-- 一次可负担性判断一行：规则版本、输入快照时点、中间计算全部可复现
CREATE TABLE IF NOT EXISTS assessments (
  id TEXT PRIMARY KEY,
  application_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  rule_set_version TEXT NOT NULL,
  decision TEXT NOT NULL,         -- approved/rejected/manual_review
  approved_limit_cents INTEGER,
  annual_rate TEXT,
  term_months INTEGER,
  triggered_reasons TEXT NOT NULL,   -- JSON
  warnings TEXT NOT NULL,            -- JSON
  consent_id TEXT NOT NULL,
  inputs TEXT NOT NULL,              -- JSON: 采用的快照 ID/时点/关键字段
  calculation TEXT NOT NULL,         -- JSON: 中间计算值
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_accounts (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  application_id TEXT,
  limit_cents INTEGER NOT NULL,
  reserved_cents INTEGER NOT NULL DEFAULT 0,
  outstanding_cents INTEGER NOT NULL DEFAULT 0,
  annual_rate TEXT NOT NULL,
  term_months INTEGER NOT NULL DEFAULT 12,
  status TEXT NOT NULL,            -- active/suspended/closed
  opened_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS limit_change_events (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  old_limit_cents INTEGER,
  new_limit_cents INTEGER NOT NULL,
  reason_code TEXT NOT NULL,
  reason_detail TEXT,
  source TEXT NOT NULL,            -- assessment/manual_review/system
  review_case_id TEXT,
  actor TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drawdowns (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  merchant_id TEXT,
  merchant_category TEXT,
  declared_purpose TEXT NOT NULL,
  status TEXT NOT NULL,
  -- reserved/evidence_pending/ready/suspended/disbursed/cancelled
  evidence TEXT,                   -- JSON: 已提交凭证及核验结果
  hold_reasons TEXT,               -- JSON: 暂停原因（信号码）
  reserved_at TEXT,
  disbursed_at TEXT,
  cancelled_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loans (
  id TEXT PRIMARY KEY,
  drawdown_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  principal_cents INTEGER NOT NULL,
  annual_rate TEXT NOT NULL,
  term_months INTEGER NOT NULL,
  start_date TEXT,
  schedule TEXT NOT NULL,          -- JSON 还款计划
  status TEXT NOT NULL,
  -- active/extended/restructured/settled/closed
  paid_principal_cents INTEGER NOT NULL DEFAULT 0,
  paid_interest_cents INTEGER NOT NULL DEFAULT 0,
  refunded_principal_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- 资金流水：商户退款/分期取消与客户还款是不同 kind，绝不混记
CREATE TABLE IF NOT EXISTS loan_events (
  id TEXT PRIMARY KEY,
  loan_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  -- disbursement/repayment/merchant_refund/installment_cancel/extension/restructure
  amount_cents INTEGER NOT NULL DEFAULT 0,
  principal_delta_cents INTEGER NOT NULL DEFAULT 0,
  detail TEXT,
  actor TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_events (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  account_id TEXT,
  drawdown_id TEXT,
  loan_id TEXT,
  code TEXT NOT NULL,
  severity TEXT NOT NULL,          -- block/warn
  payload TEXT,
  review_case_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_cases (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  account_id TEXT,
  drawdown_id TEXT,
  loan_id TEXT,
  application_id TEXT,
  signal_ids TEXT NOT NULL,       -- JSON
  topic TEXT NOT NULL,            -- underwriting/drawdown/monitoring/hardship
  status TEXT NOT NULL,           -- open/decided
  decision TEXT,                  -- limit_reduction/extension/restructure/resume/reject
  new_limit_cents INTEGER,
  new_term_months INTEGER,
  new_annual_rate TEXT,
  reason_detail TEXT,
  reviewer TEXT,
  decided_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hardship_requests (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  loan_id TEXT,
  account_id TEXT,
  requested_action TEXT NOT NULL,  -- extension/restructure/payment_holiday
  status TEXT NOT NULL,
  -- submitted/under_review/granted/rejected/completed
  evidence TEXT,
  linked_case_id TEXT,
  reviewer TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);

CREATE TABLE IF NOT EXISTS collection_contacts (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  loan_id TEXT,
  account_id TEXT,
  channel TEXT NOT NULL,
  at TEXT NOT NULL,                -- 客户当地时间（带偏移量）
  result TEXT,
  actor TEXT,
  note TEXT,
  created_at TEXT NOT NULL
);

-- 营销同意独立表：风控授权不隐含营销同意
CREATE TABLE IF NOT EXISTS marketing_grants (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  granted INTEGER NOT NULL,
  channels TEXT,
  granted_at TEXT,
  revoked_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS marketing_actions (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  allowed INTEGER NOT NULL,
  reason TEXT NOT NULL,
  actor TEXT,
  at TEXT NOT NULL
);

-- 合规/管理操作审计：谁、以什么角色、为什么读取或执行了什么
CREATE TABLE IF NOT EXISTS audit_log (
  id TEXT PRIMARY KEY,
  actor TEXT NOT NULL,
  role TEXT NOT NULL,
  action TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  detail TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_customer ON snapshot_reads(customer_id, kind, as_of);
CREATE INDEX IF NOT EXISTS idx_drawdowns_account ON drawdowns(account_id, status);
CREATE INDEX IF NOT EXISTS idx_loans_customer ON loans(customer_id);
CREATE INDEX IF NOT EXISTS idx_signals_customer ON signal_events(customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_contacts_customer ON collection_contacts(customer_id, at);
CREATE INDEX IF NOT EXISTS idx_events_loan ON loan_events(loan_id, created_at);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        # 单一连接：所有事务用一把可重入全局锁串行化，保证多线程 HTTP 下
        # 事务原子执行；customer_lock 仍承担领域级的客户串行语义。
        self._tx_lock = threading.RLock()
        self._tx_depth = threading.local()
        self._conn.executescript(SCHEMA)

    def customer_lock(self, customer_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(customer_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[customer_id] = lock
            return lock

    @contextmanager
    def transaction(self):
        """支持同线程嵌套：外层 BEGIN IMMEDIATE，内层 SAVEPOINT。"""
        depth = getattr(self._tx_depth, "depth", 0)
        if depth == 0:
            self._tx_lock.acquire()
            self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth.depth = 1
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            finally:
                self._tx_depth.depth = 0
                self._tx_lock.release()
        else:
            name = f"sp_{depth}"
            self._conn.execute(f"SAVEPOINT {name}")
            self._tx_depth.depth = depth + 1
            try:
                yield self._conn
                self._conn.execute(f"RELEASE SAVEPOINT {name}")
            except Exception:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                self._conn.execute(f"RELEASE SAVEPOINT {name}")
                raise
            finally:
                self._tx_depth.depth = depth

    # --- 通用辅助 -------------------------------------------------------

    @staticmethod
    def dumps(value) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def loads(value, default=None):
        if value is None:
            return default
        return json.loads(value)

    def insert(self, table: str, row: dict) -> dict:
        with self._tx_lock:
            cols = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row)
            self._conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", row
            )
        return row

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._tx_lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params=()):
        with self._tx_lock:
            return self._conn.execute(sql, params).fetchone()

    def get(self, table: str, row_id: str):
        return self.query_one(f"SELECT * FROM {table} WHERE id = ?", (row_id,))

    def require(self, table: str, row_id: str):
        row = self.get(table, row_id)
        if row is None:
            from credit.errors import not_found
            raise not_found(f"{table} 不存在: {row_id}")
        return row

    def row_to_dict(self, row) -> dict:
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}

    def latest_snapshot(self, customer_id: str, kind: str, as_of: str | None = None):
        """取某客户某类数据中不晚于 as_of 的最新一份不可变快照。"""
        if as_of:
            return self.query_one(
                "SELECT * FROM snapshot_reads WHERE customer_id=? AND kind=? AND as_of<=? "
                "ORDER BY as_of DESC, read_at DESC LIMIT 1",
                (customer_id, kind, as_of),
            )
        return self.query_one(
            "SELECT * FROM snapshot_reads WHERE customer_id=? AND kind=? "
            "ORDER BY as_of DESC, read_at DESC LIMIT 1",
            (customer_id, kind),
        )

    def snapshots_between(self, customer_id: str, kind: str, start: str, end: str):
        return self.query(
            "SELECT * FROM snapshot_reads WHERE customer_id=? AND kind=? "
            "AND as_of>? AND as_of<=? ORDER BY as_of",
            (customer_id, kind, start, end),
        )

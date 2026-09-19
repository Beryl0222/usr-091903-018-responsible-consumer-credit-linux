"""SQLite 持久化：所有写操作经同一连接与锁串行化，额度扣减用条件 UPDATE 保证不超额。"""

import json
import sqlite3
import threading
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS consents (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    purpose TEXT NOT NULL,
    granted INTEGER NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_consents_customer ON consents(customer_id);

CREATE TABLE IF NOT EXISTS data_snapshots (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    as_of TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_customer_kind ON data_snapshots(customer_id, kind, taken_at);

CREATE TABLE IF NOT EXISTS snapshot_access_log (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    purpose TEXT NOT NULL,
    actor TEXT NOT NULL,
    accessed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_applications (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    requested_amount_cents INTEGER NOT NULL,
    product TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS affordability_assessments (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL UNIQUE,
    customer_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    decision TEXT NOT NULL,
    max_amount_cents INTEGER NOT NULL,
    approved_limit_cents INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    inputs TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_lines (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL UNIQUE,
    total_limit_cents INTEGER NOT NULL,
    available_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS line_change_reasons (
    id TEXT PRIMARY KEY,
    line_id TEXT NOT NULL,
    change_type TEXT NOT NULL,
    old_limit_cents INTEGER,
    new_limit_cents INTEGER,
    reason_code TEXT NOT NULL,
    reason_detail TEXT NOT NULL,
    source TEXT NOT NULL,
    ref_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id TEXT PRIMARY KEY,
    line_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    merchant_id TEXT,
    merchant_name TEXT,
    usage_purpose TEXT,
    status TEXT NOT NULL,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    disbursed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_withdrawals_idem
    ON withdrawals(customer_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS hold_reasons (
    id TEXT PRIMARY KEY,
    withdrawal_id TEXT NOT NULL,
    trigger_code TEXT NOT NULL,
    detail TEXT NOT NULL,
    snapshot_as_of TEXT,
    created_at TEXT NOT NULL,
    manual_case_id TEXT
);

CREATE TABLE IF NOT EXISTS loans (
    id TEXT PRIMARY KEY,
    withdrawal_id TEXT NOT NULL UNIQUE,
    customer_id TEXT NOT NULL,
    principal_cents INTEGER NOT NULL,
    annual_rate TEXT NOT NULL,
    term_months INTEGER NOT NULL,
    first_due TEXT NOT NULL,
    status TEXT NOT NULL,
    funded_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_rows (
    id TEXT PRIMARY KEY,
    loan_id TEXT NOT NULL,
    period_no INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    principal_cents INTEGER NOT NULL,
    interest_cents INTEGER NOT NULL,
    paid_principal_cents INTEGER NOT NULL DEFAULT 0,
    paid_interest_cents INTEGER NOT NULL DEFAULT 0,
    row_status TEXT NOT NULL DEFAULT 'scheduled',
    superseded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_schedule_loan ON schedule_rows(loan_id, period_no);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    loan_id TEXT,
    kind TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    principal_balance_after_cents INTEGER NOT NULL,
    original_route TEXT,
    ref_type TEXT,
    ref_id TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_loan ON ledger_entries(loan_id, created_at);

CREATE TABLE IF NOT EXISTS payment_allocations (
    id TEXT PRIMARY KEY,
    ledger_entry_id TEXT NOT NULL,
    schedule_row_id TEXT NOT NULL,
    principal_cents INTEGER NOT NULL,
    interest_cents INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_documents (
    id TEXT PRIMARY KEY,
    withdrawal_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    claimed_amount_cents INTEGER,
    claimed_merchant TEXT,
    claimed_purpose TEXT,
    doc_status TEXT NOT NULL,
    conflict_detail TEXT,
    submitted_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS manual_cases (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    trigger_code TEXT NOT NULL,
    case_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision TEXT,
    decision_detail TEXT,
    rationale TEXT
);

CREATE TABLE IF NOT EXISTS hardship_arrangements (
    id TEXT PRIMARY KEY,
    loan_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    plan_type TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    manual_case_id TEXT,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_contacts (
    id TEXT PRIMARY KEY,
    loan_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    contact_at TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    in_grace INTEGER NOT NULL,
    hardship_hold INTEGER NOT NULL,
    blocked_reason TEXT,
    result TEXT,
    actor TEXT NOT NULL
);
"""


def new_id():
    return uuid.uuid4().hex


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Storage:
    def __init__(self, path=":memory:"):
        # isolation_level=None：显式管理事务，避免隐式事务与 BEGIN IMMEDIATE 冲突
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._in_tx = False
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)

    # ---- 基础 ----
    def execute(self, sql, params=()):
        with self._lock:
            cur = self.conn.execute(sql, params)
            if not self._in_tx:
                # SELECT 无需提交，提交亦无害
                pass
            return cur

    def commit(self):
        if not self._in_tx:
            self.conn.commit()

    def query(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def query_one(self, sql, params=()):
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def transaction(self, fn):
        """在单个事务内执行 fn(cursor)，异常回滚。

        事务内调用 self.execute / 其他写方法不会提前提交，
        全部随事务统一提交，保证"额度占用+建单"等复合操作原子。
        """
        with self._lock:
            if self._in_tx:
                # 已在事务中（同线程嵌套）：复用外层事务
                return fn(self.conn.cursor())
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self._in_tx = True
                cur = self.conn.cursor()
                result = fn(cur)
                self.conn.execute("COMMIT")
                return result
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            finally:
                self._in_tx = False

    # ---- 授权 ----
    def add_consent(self, customer_id, scope, purpose, granted_at, expires_at=None, granted=True):
        cid = new_id()
        self.execute(
            "INSERT INTO consents VALUES (?,?,?,?,?,?,?,?)",
            (cid, customer_id, scope, purpose, int(granted), granted_at, expires_at, None),
        )
        return cid

    def revoke_consent(self, customer_id, scope, revoked_at):
        self.execute(
            "UPDATE consents SET revoked_at=? WHERE customer_id=? AND scope=? AND revoked_at IS NULL",
            (revoked_at, customer_id, scope),
        )

    def active_consent(self, customer_id, scope, at_iso):
        return self.query_one(
            """
            SELECT * FROM consents
            WHERE customer_id=? AND scope=? AND granted=1
              AND granted_at<=? AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at>?)
            ORDER BY granted_at DESC LIMIT 1
            """,
            (customer_id, scope, at_iso, at_iso),
        )

    def list_consents(self, customer_id):
        return self.query(
            "SELECT * FROM consents WHERE customer_id=? ORDER BY granted_at", (customer_id,)
        )

    # ---- 快照 ----
    def add_snapshot(self, customer_id, kind, source, as_of, taken_at, payload):
        sid = new_id()
        self.execute(
            "INSERT INTO data_snapshots VALUES (?,?,?,?,?,?,?)",
            (sid, customer_id, kind, source, as_of, taken_at, _json(payload)),
        )
        return sid

    def latest_snapshot(self, customer_id, kind):
        return self.query_one(
            "SELECT * FROM data_snapshots WHERE customer_id=? AND kind=? "
            "ORDER BY taken_at DESC LIMIT 1",
            (customer_id, kind),
        )

    def snapshot_by_id(self, sid):
        return self.query_one("SELECT * FROM data_snapshots WHERE id=?", (sid,))

    def log_snapshot_access(self, customer_id, snapshot_id, scope, purpose, actor, accessed_at):
        self.execute(
            "INSERT INTO snapshot_access_log VALUES (?,?,?,?,?,?,?)",
            (new_id(), customer_id, snapshot_id, scope, purpose, actor, accessed_at),
        )

    def list_snapshot_access(self, customer_id):
        return self.query(
            "SELECT * FROM snapshot_access_log WHERE customer_id=? ORDER BY accessed_at",
            (customer_id,),
        )

    # ---- 申请与评估 ----
    def create_application(self, customer_id, amount_cents, product, now):
        aid = new_id()
        self.execute(
            "INSERT INTO credit_applications VALUES (?,?,?,?,?,?,?)",
            (aid, customer_id, amount_cents, product, "submitted", now, now),
        )
        return aid

    def get_application(self, aid):
        return self.query_one("SELECT * FROM credit_applications WHERE id=?", (aid,))

    def save_assessment(self, application_id, customer_id, assessment, now):
        self.execute(
            "INSERT INTO affordability_assessments VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                new_id(),
                application_id,
                customer_id,
                assessment["rule_version"],
                assessment["decision"],
                assessment["max_amount_cents"],
                assessment["approved_limit_cents"],
                _json(assessment["reasons"]),
                _json(assessment["inputs"]),
                now,
            ),
        )

    def get_assessment_by_application(self, application_id):
        return self.query_one(
            "SELECT * FROM affordability_assessments WHERE application_id=?", (application_id,)
        )

    def set_application_status(self, aid, status, now):
        self.execute(
            "UPDATE credit_applications SET status=?, updated_at=? WHERE id=?", (status, now, aid)
        )

    # ---- 额度 ----
    def get_line(self, customer_id):
        return self.query_one("SELECT * FROM credit_lines WHERE customer_id=?", (customer_id,))

    def create_line(self, customer_id, limit_cents, now):
        lid = new_id()
        self.execute(
            "INSERT INTO credit_lines VALUES (?,?,?,?,?,?,?)",
            (lid, customer_id, limit_cents, limit_cents, "active", now, now),
        )
        return lid

    def reserve_available(self, line_id, amount_cents):
        """原子条件扣减：可用额度不足时返回 False（并发提款安全的关键）。"""
        cur = self.execute(
            "UPDATE credit_lines SET available_cents=available_cents-?, updated_at=updated_at "
            "WHERE id=? AND status='active' AND available_cents>=?",
            (amount_cents, line_id, amount_cents),
        )
        return cur.rowcount == 1

    def release_available(self, line_id, amount_cents):
        self.execute(
            "UPDATE credit_lines SET available_cents=available_cents+? WHERE id=? "
            "AND available_cents+? <= total_limit_cents",
            (amount_cents, line_id, amount_cents),
        )

    def set_line_status(self, line_id, status, now):
        self.execute(
            "UPDATE credit_lines SET status=?, updated_at=? WHERE id=?", (status, now, line_id)
        )

    def adjust_limit(self, line_id, new_limit_cents, now):
        line = self.query_one("SELECT * FROM credit_lines WHERE id=?", (line_id,))
        used = line["total_limit_cents"] - line["available_cents"]
        new_available = max(0, new_limit_cents - used)
        self.execute(
            "UPDATE credit_lines SET total_limit_cents=?, available_cents=?, updated_at=? WHERE id=?",
            (new_limit_cents, new_available, now, line_id),
        )
        return line["total_limit_cents"], new_available

    def add_line_change(self, line_id, change_type, old_limit, new_limit, reason_code,
                        reason_detail, source, ref_id, now):
        self.execute(
            "INSERT INTO line_change_reasons VALUES (?,?,?,?,?,?,?,?,?,?)",
            (new_id(), line_id, change_type, old_limit, new_limit, reason_code,
             reason_detail, source, ref_id, now),
        )

    def list_line_changes(self, line_id):
        return self.query(
            "SELECT * FROM line_change_reasons WHERE line_id=? ORDER BY created_at", (line_id,)
        )

    # ---- 提款 ----
    def create_withdrawal(self, line_id, customer_id, amount_cents, merchant, purpose, now,
                          idem_key, status="reserved"):
        wid = new_id()
        self.execute(
            "INSERT INTO withdrawals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wid, line_id, customer_id, amount_cents, "CNY",
             merchant.get("id") if merchant else None,
             merchant.get("name") if merchant else None,
             purpose, status, idem_key, now, now, None),
        )
        return wid

    def get_withdrawal(self, wid):
        return self.query_one("SELECT * FROM withdrawals WHERE id=?", (wid,))

    def find_withdrawal_by_idem(self, customer_id, idem_key):
        return self.query_one(
            "SELECT * FROM withdrawals WHERE customer_id=? AND idempotency_key=?",
            (customer_id, idem_key),
        )

    def set_withdrawal_status(self, wid, status, now, disbursed_at=None):
        self.execute(
            "UPDATE withdrawals SET status=?, updated_at=?, disbursed_at=COALESCE(?, disbursed_at) "
            "WHERE id=?",
            (status, now, disbursed_at, wid),
        )

    def add_hold_reason(self, wid, trigger_code, detail, snapshot_as_of, now, case_id=None):
        hid = new_id()
        self.execute(
            "INSERT INTO hold_reasons VALUES (?,?,?,?,?,?,?)",
            (hid, wid, trigger_code, _json(detail), snapshot_as_of, now, case_id),
        )
        return hid

    def list_hold_reasons(self, wid):
        return self.query("SELECT * FROM hold_reasons WHERE withdrawal_id=? ORDER BY created_at", (wid,))

    def resolve_hold_reasons(self, wid, case_id):
        self.execute("UPDATE hold_reasons SET manual_case_id=? WHERE withdrawal_id=?", (case_id, wid))

    # ---- 贷款与计划 ----
    def create_loan(self, wid, customer_id, principal_cents, annual_rate, term_months,
                    first_due, funded_at, now):
        lid = new_id()
        self.execute(
            "INSERT INTO loans VALUES (?,?,?,?,?,?,?,?,?,?)",
            (lid, wid, customer_id, principal_cents, str(annual_rate), term_months,
             first_due, "repaying", funded_at, now),
        )
        return lid

    def get_loan(self, lid):
        return self.query_one("SELECT * FROM loans WHERE id=?", (lid,))

    def get_loan_by_withdrawal(self, wid):
        return self.query_one("SELECT * FROM loans WHERE withdrawal_id=?", (wid,))

    def set_loan_status(self, lid, status):
        self.execute("UPDATE loans SET status=? WHERE id=?", (status, lid))

    def insert_schedule_row(self, loan_id, period_no, due, principal, interest):
        self.execute(
            "INSERT INTO schedule_rows VALUES (?,?,?,?,?,?,?,?,?,?)",
            (new_id(), loan_id, period_no, due, principal, interest, 0, 0, "scheduled", 0),
        )

    def schedule_rows(self, loan_id, active_only=True):
        sql = "SELECT * FROM schedule_rows WHERE loan_id=?"
        if active_only:
            sql += " AND superseded=0"
        sql += " ORDER BY period_no"
        return self.query(sql, (loan_id,))

    def supersede_schedule(self, loan_id):
        self.execute(
            "UPDATE schedule_rows SET superseded=1, row_status='cancelled' "
            "WHERE loan_id=? AND superseded=0",
            (loan_id,),
        )

    def supersede_fully_unpaid_schedule(self, loan_id):
        """只作废完全未还的期次；已还清或部分还款的期次保留不动。"""
        self.execute(
            "UPDATE schedule_rows SET superseded=1, row_status='cancelled' "
            "WHERE loan_id=? AND superseded=0 "
            "AND paid_principal_cents=0 AND paid_interest_cents=0",
            (loan_id,),
        )

    def shift_unpaid_schedule(self, loan_id, months):
        """把所有未结清期次的到期日顺延 months 个月（困难缓还，金额不变）。"""
        from .util import add_months
        from datetime import date

        rows = self.query(
            "SELECT * FROM schedule_rows WHERE loan_id=? AND superseded=0 AND "
            "(paid_principal_cents<principal_cents OR paid_interest_cents<interest_cents)",
            (loan_id,),
        )
        for r in rows:
            new_due = add_months(date.fromisoformat(r["due_date"]), months).isoformat()
            self.execute(
                "UPDATE schedule_rows SET due_date=? WHERE id=?", (new_due, r["id"])
            )

    def update_schedule_row_paid(self, row_id, principal, interest, status):
        self.execute(
            "UPDATE schedule_rows SET paid_principal_cents=?, paid_interest_cents=?, row_status=? "
            "WHERE id=?",
            (principal, interest, status, row_id),
        )

    # ---- 台账 ----
    def add_ledger_entry(self, customer_id, loan_id, kind, amount_cents, balance_after,
                         created_at, original_route=None, ref_type=None, ref_id=None, detail=None):
        eid = new_id()
        self.execute(
            "INSERT INTO ledger_entries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (eid, customer_id, loan_id, kind, amount_cents, balance_after, original_route,
             ref_type, ref_id, _json(detail) if detail is not None else None, created_at),
        )
        return eid

    def ledger_entries(self, loan_id=None, customer_id=None):
        sql = "SELECT * FROM ledger_entries WHERE 1=1"
        params = []
        if loan_id:
            sql += " AND loan_id=?"
            params.append(loan_id)
        if customer_id:
            sql += " AND customer_id=?"
            params.append(customer_id)
        sql += " ORDER BY created_at, rowid"
        return self.query(sql, params)

    def add_allocation(self, entry_id, row_id, principal, interest):
        self.execute(
            "INSERT INTO payment_allocations VALUES (?,?,?,?,?)",
            (new_id(), entry_id, row_id, principal, interest),
        )

    def allocations_for_entry(self, entry_id):
        return self.query("SELECT * FROM payment_allocations WHERE ledger_entry_id=?", (entry_id,))

    # ---- 用途核验 ----
    def add_usage_document(self, wid, customer_id, doc, now):
        did = new_id()
        self.execute(
            "INSERT INTO usage_documents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (did, wid, customer_id, doc["doc_type"], doc.get("claimed_amount_cents"),
             doc.get("claimed_merchant"), doc.get("claimed_purpose"),
             doc["doc_status"], _json(doc.get("conflict_detail")) if doc.get("conflict_detail") else None,
             now, doc.get("reviewed_at")),
        )
        return did

    def usage_documents(self, wid):
        return self.query(
            "SELECT * FROM usage_documents WHERE withdrawal_id=? ORDER BY submitted_at", (wid,)
        )

    # ---- 人工案件 ----
    def create_manual_case(self, customer_id, subject_type, subject_id, trigger_code, now):
        mid = new_id()
        self.execute(
            "INSERT INTO manual_cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, customer_id, subject_type, subject_id, trigger_code, "open", now,
             None, None, None, None, None),
        )
        return mid

    def get_manual_case(self, mid):
        return self.query_one("SELECT * FROM manual_cases WHERE id=?", (mid,))

    def open_case_for_subject(self, subject_type, subject_id):
        return self.query_one(
            "SELECT * FROM manual_cases WHERE subject_type=? AND subject_id=? AND case_status='open' "
            "ORDER BY created_at DESC LIMIT 1",
            (subject_type, subject_id),
        )

    def decide_manual_case(self, mid, decided_by, decision, detail, rationale, now):
        self.execute(
            "UPDATE manual_cases SET case_status='decided', decided_at=?, decided_by=?, "
            "decision=?, decision_detail=?, rationale=? WHERE id=?",
            (now, decided_by, decision, _json(detail), rationale, mid),
        )

    def list_manual_cases(self, customer_id=None, status=None):
        sql = "SELECT * FROM manual_cases WHERE 1=1"
        params = []
        if customer_id:
            sql += " AND customer_id=?"
            params.append(customer_id)
        if status:
            sql += " AND case_status=?"
            params.append(status)
        sql += " ORDER BY created_at"
        return self.query(sql, params)

    # ---- 困难协商 ----
    def add_arrangement(self, loan_id, customer_id, plan_type, status, requested_at, detail,
                        case_id=None, decided_at=None, decided_by=None):
        aid = new_id()
        self.execute(
            "INSERT INTO hardship_arrangements VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, loan_id, customer_id, plan_type, status, requested_at, decided_at,
             decided_by, case_id, _json(detail)),
        )
        return aid

    def get_arrangement(self, aid):
        row = self.query_one("SELECT * FROM hardship_arrangements WHERE id=?", (aid,))
        return row

    def active_arrangement(self, loan_id):
        return self.query_one(
            "SELECT * FROM hardship_arrangements WHERE loan_id=? AND status IN ('requested','active') "
            "ORDER BY requested_at DESC LIMIT 1",
            (loan_id,),
        )

    def set_arrangement_status(self, aid, status, decided_at=None, decided_by=None, detail=None):
        if detail is not None:
            self.execute(
                "UPDATE hardship_arrangements SET status=?, decided_at=?, decided_by=?, detail=? "
                "WHERE id=?",
                (status, decided_at, decided_by, _json(detail), aid),
            )
        else:
            self.execute(
                "UPDATE hardship_arrangements SET status=?, decided_at=?, decided_by=? WHERE id=?",
                (status, decided_at, decided_by, aid),
            )

    # ---- 催收联系 ----
    def add_collection_contact(self, loan_id, customer_id, channel, contact_at, allowed,
                               in_grace, hardship_hold, actor, blocked_reason=None, result=None):
        cid = new_id()
        self.execute(
            "INSERT INTO collection_contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cid, loan_id, customer_id, channel, contact_at, int(allowed), int(in_grace),
             int(hardship_hold), blocked_reason, result, actor),
        )
        return cid

    def collection_contacts(self, customer_id):
        return self.query(
            "SELECT * FROM collection_contacts WHERE customer_id=? ORDER BY contact_at",
            (customer_id,),
        )

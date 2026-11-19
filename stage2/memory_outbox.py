"""UAI 记忆投递层：SQLite durable outbox。

图谱是派生数据，但「写丢」不能被静默吞掉。本模块保证：
进程崩溃不丢（落盘）、重试有退避、超限进死信、死信可重放、
inflight 超时可回收。它不关心图谱长什么样，只负责把 intent 送到 writer。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

MAX_ATTEMPTS = 5
BACKOFF_S = (1, 2, 4, 8, 16)
STALE_INFLIGHT_S = 120
# 入队必须快：它跑在裁定响应路径上，不能吃掉 hub 的等待预算
ENQUEUE_ATTEMPTS = 2
ENQUEUE_BUSY_TIMEOUT_MS = 1000
WARNINGS: list[str] = []

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    intent_id       TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    task_id         TEXT,
    subject         TEXT,
    claim           TEXT,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error      TEXT,
    degraded        INTEGER NOT NULL DEFAULT 0,
    verified        TEXT,
    write_path      TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    delivered_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_intents_due  ON intents(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_intents_task ON intents(task_id);
"""


def default_db_path() -> Path:
    # 允许测试/多环境指向独立库：否则测试会往生产 outbox 里写垃圾
    override = os.environ.get("UAI_MEMORY_DB")
    if override:
        return Path(override)
    return Path.home() / ".shared" / "graph-memory" / "outbox.db"


class MemoryOutbox:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # 顺序很重要：先拿 busy_timeout，再切 WAL。
        # journal_mode=WAL 在库被别的写事务占着时会**立即**抛 database is locked，
        # 不享受 busy_timeout；先设超时能让后续语句等待而不是秒失败。
        conn.execute("PRAGMA busy_timeout=%d" % busy_timeout_ms)
        try:
            conn.execute("PRAGMA secure_delete=ON")     # purge 后不留明文页
        except sqlite3.OperationalError:
            WARNINGS.append("secure_delete_unavailable")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # 切不过去就继续用现有 journal 模式；但必须留信号，不能静默退化
            WARNINGS.append("wal_unavailable")
        return conn

    # ---------- write ----------

    def enqueue(self, intent: dict[str, Any]) -> dict[str, Any]:
        from memory_intent import validate_intent

        reason = validate_intent(intent)
        if reason:
            return {"enqueued": False, "duplicate": False, "reason": reason}
        now = time.time()
        payload = json.dumps(intent, ensure_ascii=False, sort_keys=True)
        last_error = None
        for attempt in range(ENQUEUE_ATTEMPTS):   # 写锁竞争时重试，但预算要小
            try:
                with self._connect(ENQUEUE_BUSY_TIMEOUT_MS) as conn:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO intents "
                        "(intent_id, kind, task_id, subject, claim, payload, status, "
                        " attempts, next_attempt_at, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,'pending',0,0,?,?)",
                        (
                            intent["intent_id"],
                            intent["kind"],
                            intent.get("task_id"),
                            intent.get("subject"),
                            intent.get("claim"),
                            payload,
                            now,
                            now,
                        ),
                    )
                    if cur.rowcount == 0:
                        return {"enqueued": False, "duplicate": True,
                                "reason": "duplicate_intent_id"}
                return {"enqueued": True, "duplicate": False, "reason": None}
            except sqlite3.OperationalError as exc:
                last_error = exc
                time.sleep(0.02 * (attempt + 1))
        return {"enqueued": False, "duplicate": False,
                "reason": "outbox_unavailable:%s" % str(last_error)[:120]}

    def claim_due(self, limit: int = 10, now: float | None = None) -> list[dict[str, Any]]:
        """原子领取：BEGIN IMMEDIATE + 逐行条件更新。

        旧实现 SELECT 与 UPDATE 不在同一事务（autocommit），多线程/多进
        下同一条会被领走多次。这里用写锁串行化，并用 `status IN (...)`
        条件更新兜住「刚被别的执行者改过」的窗口。
        """
        stamp = time.time() if now is None else now
        conn = self._connect()
        claimed: list[dict[str, Any]] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM intents "
                "WHERE status IN ('pending','retry') AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at ASC, created_at ASC LIMIT ?",
                (stamp, int(limit)),
            ).fetchall()
            for row in rows:
                cur = conn.execute(
                    "UPDATE intents SET status='inflight', updated_at=? "
                    "WHERE intent_id=? AND status IN ('pending','retry')",
                    (time.time(), row["intent_id"]),
                )
                if not cur.rowcount:
                    continue          # 别人刚领走
                item = dict(row)
                item["status"] = "inflight"
                item["intent"] = json.loads(item["payload"])
                claimed.append(item)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return claimed

    def mark_delivered(
        self,
        intent_id: str,
        *,
        degraded: bool = False,
        verified: str | None = None,
        write_path: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE intents SET status='delivered', degraded=?, verified=?, "
                "write_path=?, last_error=?, updated_at=?, delivered_at=? "
                "WHERE intent_id=? AND status IN ('inflight','delivered')",
                (1 if degraded else 0, verified, write_path, last_error, now, now, intent_id),
            )
            # 返回是否真的落库：行可能已被 recover_stale 打回 retry，静默丢更新会让
            # drain 以为写成功而实际下一轮会重复投递
            return bool(cur.rowcount)

    def mark_failed(self, intent_id: str, error: str, *, now: float | None = None) -> str:
        """只对 inflight 生效：已投递的行不能被迟到的失败回调回滚。"""
        stamp = time.time() if now is None else now
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempts, status FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if not row or row["status"] != "inflight":
                conn.execute("COMMIT")
                return "ignored"
            attempts = int(row["attempts"]) + 1
            if attempts >= MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE intents SET status='dead_letter', attempts=?, last_error=?, "
                    "updated_at=? WHERE intent_id=? AND status='inflight'",
                    (attempts, str(error)[:400], stamp, intent_id),
                )
                conn.execute("COMMIT")
                return "dead_letter"
            delay = BACKOFF_S[min(attempts - 1, len(BACKOFF_S) - 1)]
            conn.execute(
                "UPDATE intents SET status='retry', attempts=?, last_error=?, "
                "next_attempt_at=?, updated_at=? WHERE intent_id=? AND status='inflight'",
                (attempts, str(error)[:400], stamp + delay, stamp, intent_id),
            )
            conn.execute("COMMIT")
            return "retry"
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def recover_stale(self, older_than_s: float = STALE_INFLIGHT_S,
                      now: float | None = None) -> int:
        stamp = time.time() if now is None else now
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE intents SET status='retry', next_attempt_at=?, updated_at=? "
                "WHERE status='inflight' AND updated_at < ?",
                (stamp, stamp, stamp - older_than_s),
            )
            return int(cur.rowcount or 0)

    def purge(self, intent_id: str, reason: str) -> bool:
        """彻底丢弃一条 intent：内容不像记忆（例如含凭据）时不能留在库里。

        UPDATE 只改逻辑页，明文仍可能在空闲页/WAL 帧里。secure_delete=ON
        负责把释放的页抹掉，再 VACUUM 一次把库重写干净。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE intents SET status='dead_letter', payload='{}', subject='', "
                "claim='', last_error=?, updated_at=? WHERE intent_id=?",
                (str(reason)[:400], time.time(), intent_id),
            )
            changed = bool(cur.rowcount)
        if changed:
            try:
                with self._connect() as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("VACUUM")
            except sqlite3.OperationalError:
                WARNINGS.append("purge_vacuum_failed")
        return changed

    def replay(self, intent_id: str) -> bool:
        """只允许重放未在投递中的行：把 inflight 打回 pending 会造出第二个写者。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE intents SET status='pending', attempts=0, next_attempt_at=0, "
                "last_error=NULL, updated_at=? "
                "WHERE intent_id=? AND status IN ('dead_letter','retry','pending')",
                (time.time(), intent_id),
            )
            return int(cur.rowcount or 0) > 0

    # ---------- read ----------

    def get(self, intent_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
        return dict(row) if row else None

    def dead_letters(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM intents WHERE status='dead_letter' "
                "ORDER BY updated_at ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def degraded_rows(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM intents WHERE degraded=1 ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def task_ids(self) -> set[str]:
        """任何状态的 intent 覆盖到的 task_id（含未投递）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT task_id FROM intents WHERE task_id IS NOT NULL"
            ).fetchall()
        return {row["task_id"] for row in rows}

    def delivered_task_ids(self) -> set[str]:
        """**真正进了图**的 task_id。pending/retry/dead 都不算有记忆。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT task_id FROM intents "
                "WHERE task_id IS NOT NULL AND status='delivered'"
            ).fetchall()
        return {row["task_id"] for row in rows}

    def verified_task_ids(self) -> set[str]:
        """既是 delivered 又通过回读确认的 task_id：审计只认这个口径。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT task_id FROM intents "
                "WHERE task_id IS NOT NULL AND status='delivered' AND verified='verified'"
            ).fetchall()
        return {row["task_id"] for row in rows}

    def unverified_rows(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM intents WHERE status='delivered' AND verified<>'verified' "
                "ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM intents GROUP BY status"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) AS n FROM intents").fetchone()["n"]
            degraded = conn.execute(
                "SELECT COUNT(*) AS n FROM intents WHERE degraded=1"
            ).fetchone()["n"]
            unverified = conn.execute(
                "SELECT COUNT(*) AS n FROM intents WHERE verified='unverified'"
            ).fetchone()["n"]
        out: dict[str, Any] = {
            "total": int(total),
            "pending": 0,
            "inflight": 0,
            "retry": 0,
            "delivered": 0,
            "dead_letter": 0,
        }
        for row in rows:
            out[row["status"]] = int(row["n"])
        out["degraded"] = int(degraded)
        out["unverified"] = int(unverified)
        return out

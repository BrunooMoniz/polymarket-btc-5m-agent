"""Persistência: uma linha por janela (SQLite) para reinício não reentrar, e journal JSONL
com toda a evidência por decisão (calibração posterior)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# "orphan": ordem cujo destino não foi confirmado (cancelamento sem resposta). Fecha a janela para
# novas ordens e fica na fila de resolução até o CLOB dizer se executou.
# "closed": posição vendida antes do fim da janela (saída antecipada). PnL já realizado.
FINAL_STATUSES = ("filled", "unfilled", "skipped", "settled", "orphan", "closed")
REALIZED_STATUSES = ("settled", "closed")
OPEN_ORDER_STATUSES = ("quoting", "orphan")

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    ts INTEGER PRIMARY KEY,
    slug TEXT,
    mode TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    side TEXT,
    limit_price REAL,
    shares REAL,
    cost_usd REAL,
    order_id TEXT,
    filled_shares REAL DEFAULT 0,
    fill_price REAL,
    requotes INTEGER DEFAULT 0,
    jev_calls INTEGER DEFAULT 0,
    strike REAL,
    close_price REAL,
    outcome TEXT,
    pnl_usd REAL,
    p_model REAL,
    outcome_source TEXT,
    reconciled INTEGER DEFAULT 0,
    partial_pnl_usd REAL DEFAULT 0,
    exit_price REAL,
    exit_order_id TEXT,
    exit_pending INTEGER DEFAULT 0,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
MIGRATIONS = (
    "ALTER TABLE windows ADD COLUMN outcome_source TEXT",
    "ALTER TABLE windows ADD COLUMN reconciled INTEGER DEFAULT 0",
    "ALTER TABLE windows ADD COLUMN partial_pnl_usd REAL DEFAULT 0",  # PnL já realizado em vendas parciais
    "ALTER TABLE windows ADD COLUMN exit_price REAL",                 # preço do token na saída (NÃO é BTC)
    "ALTER TABLE windows ADD COLUMN exit_order_id TEXT",
    "ALTER TABLE windows ADD COLUMN exit_pending INTEGER DEFAULT 0",
)


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class Ledger:
    def __init__(self, db_path: Path, journal_path: Path):
        self.db_path = Path(db_path)
        self.journal_path = Path(journal_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                with self._conn:
                    self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # coluna já existe

    # --- janelas ---------------------------------------------------------
    def get(self, ts: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM windows WHERE ts = ?", (ts,)).fetchone()
        return dict(row) if row else None

    def upsert(self, ts: int, **fields: Any) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            row = self._conn.execute("SELECT * FROM windows WHERE ts = ?", (ts,)).fetchone()
            if row is None:
                fields.setdefault("status", "seen")
                cols = ["ts", "created_at", "updated_at"] + list(fields.keys())
                vals = [ts, now, now] + list(fields.values())
                with self._conn:
                    self._conn.execute(
                        f"INSERT INTO windows ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", vals
                    )
            else:
                fields["updated_at"] = now
                sets = ", ".join(f"{k} = ?" for k in fields)
                with self._conn:
                    self._conn.execute(f"UPDATE windows SET {sets} WHERE ts = ?", list(fields.values()) + [ts])
            row = self._conn.execute("SELECT * FROM windows WHERE ts = ?", (ts,)).fetchone()
        return dict(row)

    def is_final(self, ts: int) -> bool:
        row = self.get(ts)
        return bool(row and row["status"] in FINAL_STATUSES)

    def filled_unsettled(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM windows WHERE status = 'filled' ORDER BY ts").fetchall()
        return [dict(r) for r in rows]

    def settled_unreconciled(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM windows WHERE status = 'settled' AND COALESCE(reconciled,0) = 0 ORDER BY ts"
            ).fetchall()
        return [dict(r) for r in rows]

    def exit_pending_rows(self) -> List[Dict[str, Any]]:
        """Vendas de saída antecipada sem destino confirmado: enquanto existirem, a janela não tenta
        vender de novo (venderia o que talvez já tenha sido vendido)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM windows WHERE COALESCE(exit_pending,0) = 1 AND exit_order_id IS NOT NULL ORDER BY ts"
            ).fetchall()
        return [dict(r) for r in rows]

    def open_order_rows(self) -> List[Dict[str, Any]]:
        """Janelas com ordem possivelmente viva no CLOB: 'quoting' (processo morreu ou o poll
        levantou no meio do TTL) e 'orphan' (cancelamento não confirmado)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM windows WHERE status IN ('quoting','orphan') AND order_id IS NOT NULL ORDER BY ts"
            ).fetchall()
        return [dict(r) for r in rows]

    def missing_outcome(self, before_ts: int, limit: int) -> List[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts FROM windows WHERE outcome IS NULL AND status IN ('seen','skipped','unfilled','closed') AND ts < ? "
                "ORDER BY ts DESC LIMIT ?", (before_ts, limit)
            ).fetchall()
        return [int(r[0]) for r in rows]

    def last_trade_update(self) -> float:
        with self._lock:
            v = self._conn.execute(
                "SELECT COALESCE(MAX(updated_at),0) FROM windows WHERE status IN ('quoting','orphan','filled','settled')"
            ).fetchone()[0]
        return float(v or 0.0)

    def day_stats(self, day: str) -> Dict[str, Any]:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        with self._lock:
            rows = self._conn.execute("SELECT status, pnl_usd, cost_usd, partial_pnl_usd FROM windows WHERE ts >= ? AND ts < ?", (start, start + 86400)).fetchall()
        settled = [r for r in rows if r["status"] in REALIZED_STATUSES]
        return {
            "windows": len(rows),
            "settled": len(settled),
            "wins": sum(1 for r in settled if (r["pnl_usd"] or 0) > 0),
            "pnl": round(sum(r["pnl_usd"] or 0 for r in settled) + sum(r["partial_pnl_usd"] or 0 for r in rows), 2),
            "staked": round(sum(r["cost_usd"] or 0 for r in settled), 2),
            "open": sum(1 for r in rows if r["status"] in ("filled", "quoting", "orphan")),
            "closed_early": sum(1 for r in rows if r["status"] == "closed"),
        }

    def meta_get(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def open_cost_usd(self) -> float:
        with self._lock:
            v = self._conn.execute(
                "SELECT COALESCE(SUM(COALESCE(filled_shares,0) * COALESCE(fill_price,0)),0) FROM windows WHERE status = 'filled'"
            ).fetchone()[0]
        return float(v or 0.0)

    def realized_pnl_usd(self, day: Optional[str] = None) -> float:
        with self._lock:
            # Venda parcial realiza caixa antes de a janela fechar: entra no dia e no stop diário.
            if day is None:
                v = self._conn.execute(
                    "SELECT COALESCE(SUM(CASE WHEN status IN ('settled','closed') THEN COALESCE(pnl_usd,0) ELSE 0 END)"
                    " + SUM(COALESCE(partial_pnl_usd,0)), 0) FROM windows"
                ).fetchone()[0]
            else:
                start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
                v = self._conn.execute(
                    "SELECT COALESCE(SUM(CASE WHEN status IN ('settled','closed') THEN COALESCE(pnl_usd,0) ELSE 0 END)"
                    " + SUM(COALESCE(partial_pnl_usd,0)), 0) FROM windows WHERE ts >= ? AND ts < ?",
                    (start, start + 86400),
                ).fetchone()[0]
        return float(v or 0.0)

    def all_rows(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM windows ORDER BY ts").fetchall()
        return [dict(r) for r in rows]

    # --- journal ---------------------------------------------------------
    def journal(self, event: str, **data: Any) -> None:
        rec = {"t": round(time.time(), 3), "event": event}
        rec.update(data)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            with self.journal_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

import json

import pytest

from src.ledger import Ledger, day_of


def test_one_row_per_window_survives_restart(tmp_path):
    db = tmp_path / "l.sqlite"
    j = tmp_path / "j.jsonl"
    l1 = Ledger(db, j)
    l1.upsert(1789697400, status="filled", side="Up", filled_shares=5, fill_price=0.48, cost_usd=2.4, strike=76643.0)
    assert l1.is_final(1789697400)
    l1.journal("fill", ts=1789697400, x=1)

    l2 = Ledger(db, j)  # "reinício"
    row = l2.get(1789697400)
    assert row["status"] == "filled" and row["side"] == "Up"
    assert l2.is_final(1789697400)
    assert l2.filled_unsettled()[0]["ts"] == 1789697400
    assert l2.open_cost_usd() == 2.4
    assert json.loads(j.read_text().splitlines()[0])["event"] == "fill"


def test_pnl_by_day_and_open_cost(tmp_path):
    l = Ledger(tmp_path / "l.sqlite", tmp_path / "j.jsonl")
    l.upsert(1789697400, status="settled", pnl_usd=-2.4)
    l.upsert(1789697700, status="settled", pnl_usd=2.6)
    l.upsert(1789611000, status="settled", pnl_usd=-9.0)  # dia anterior
    assert l.realized_pnl_usd() == pytest.approx(0.2 - 9.0)
    assert l.realized_pnl_usd(day_of(1789697400)) == pytest.approx(0.2)
    assert l.realized_pnl_usd(day_of(1789611000)) == pytest.approx(-9.0)
    assert l.filled_unsettled() == []
    l.upsert(1789698000, status="filled", filled_shares=5.0, fill_price=0.5)
    assert l.open_cost_usd() == 2.5


def test_upsert_keeps_other_fields(tmp_path):
    l = Ledger(tmp_path / "l.sqlite", tmp_path / "j.jsonl")
    l.upsert(1, status="seen", strike=1.5)
    l.upsert(1, jev_calls=2)
    r = l.get(1)
    assert r["strike"] == 1.5 and r["jev_calls"] == 2 and r["status"] == "seen"

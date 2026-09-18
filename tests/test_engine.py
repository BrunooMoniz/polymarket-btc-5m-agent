"""Loop da janela com dublês offline: mercado, book, Price to Beat, feed, Jev e broker paper."""
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.chainlink_feed import PriceBuffer
from src.config import Settings
from src.engine_5m import Engine
from src.execution_5m import PaperBroker
from src.jev_5m import JevGate, parse_response
from src.ledger import Ledger
from src.polymarket_5m import Book, Market5m, PriceToBeat

TS = 1789697400
STRIKE = 76643.29
TU, TD = "TOKEN_UP", "TOKEN_DOWN"


class Clock:
    def __init__(self, now):
        self.now = float(now)

    def __call__(self):
        return self.now

    def sleep(self, dt):
        self.now += dt


class FakePM:
    def __init__(self):
        self.market_obj = Market5m(
            slug=f"btc-updown-5m-{TS}", ts_start=TS, ts_end=TS + 300, market_id="1", condition_id="0x1",
            token_up=TU, token_down=TD, tick=0.01, min_size=5, fee_rate=0.07, accepting_orders=True, closed=False,
        )
        self.strike = STRIKE
        self.completed = None  # (open, close) quando liquidado
        self.books = {TU: (0.60, 0.62), TD: (0.37, 0.40)}
        self.book_calls = 0
        self.after_calls = None  # (n, token, (bid, ask)) muda o book após n consultas

    def market(self, ts):
        return self.market_obj if ts == TS else None

    def price_to_beat(self, ts):
        if self.completed:
            return PriceToBeat(self.completed[0], self.completed[1], True)
        return PriceToBeat(self.strike, None, False) if self.strike else None

    def book(self, token):
        self.book_calls += 1
        if self.after_calls and self.book_calls > self.after_calls[0] and token == self.after_calls[1]:
            bid, ask = self.after_calls[2]
        else:
            bid, ask = self.books[token]
        return Book(best_bid=bid, best_ask=ask, bid_size=100, ask_size=100, ts=0.0)

    def gamma_outcome(self, ts):
        return None


def make_feed(now, delta=+60.0, sigma_1s=5e-5):
    """Série de 900 s terminando em strike+delta com vol por segundo aproximada."""
    b = PriceBuffer()
    px = STRIKE + delta
    vals = []
    for i in range(900):
        t = now - i
        vals.append((t, px))
        px *= math.exp((-sigma_1s) if i % 2 == 0 else sigma_1s)
    b.add_many(vals)
    return b


def fake_jev(anomaly=0.1, direction=0.8, probs=None):
    probs = probs or {0: 0.1, 1: 0.3, 2: 0.6}

    class C:
        calls = 0

        def system_one(self, state, questions):
            C.calls += 1
            return SimpleNamespace(
                scores={"regime": SimpleNamespace(score=1.5, probabilities=probs, confidence=0.6)},
                nouls={"anomaly": SimpleNamespace(noul=anomaly), "direction_up": SimpleNamespace(noul=direction)},
            )

    gate = JevGate(client_factory=lambda: C())
    gate._cls = C
    return gate


def build(tmp_path, clock, pm=None, feed=None, jev=None, **over):
    s = Settings(data_dir=Path(tmp_path), **over)
    pm = pm or FakePM()
    feed = feed if feed is not None else make_feed(clock.now)
    jev = jev or fake_jev()
    ledger = Ledger(s.ledger_path, s.journal_path)
    broker = PaperBroker(pm.book, s.paper_bankroll_usd)
    eng = Engine(s, pm, feed, jev, broker, ledger, clock=clock, sleep=clock.sleep, seed_sigma_1s=5e-5)
    return eng, pm, ledger, jev


def test_outside_operational_band(tmp_path):
    for phase in (0, 10, 29, 276, 299):
        clock = Clock(TS + phase)
        eng, *_ = build(tmp_path / str(phase), clock)
        assert eng.step(clock.now) == "outside"


def test_full_window_fill_and_settle(tmp_path):
    clock = Clock(TS + 120)
    eng, pm, ledger, jev = build(tmp_path, clock)
    pm.after_calls = (4, TU, (0.60, 0.61))  # 2 books da decisão + 1 refetch antes do post; o ask cai na 1ª sondagem
    out = eng.step(clock.now)
    assert out == "filled"
    row = ledger.get(TS)
    assert row["status"] == "filled" and row["side"] == "Up" and row["limit_price"] == pytest.approx(0.61)
    assert row["filled_shares"] == pytest.approx(8.19)  # 5,00 / 0,61
    assert jev._cls.calls == 1

    # reinício no meio da janela: não reentra (só marca a posição a mercado)
    eng2, _, ledger2, _ = build(tmp_path, clock, pm=pm)
    assert eng2.step(clock.now) == "holding"
    assert ledger2.get(TS)["status"] == "filled"

    # liquidação: empate resolve Up
    pm.completed = (STRIKE, STRIKE)
    clock.now = TS + 300 + 30
    assert eng.settle_pending(clock.now) == 1
    row = ledger.get(TS)
    assert row["status"] == "settled" and row["outcome"] == "Up"
    assert row["pnl_usd"] == pytest.approx(8.19 * (1 - 0.61), abs=1e-3)
    assert ledger.realized_pnl_usd() > 0

    events = [l.split('"event": "')[1].split('"')[0] for l in ledger.journal_path.read_text().splitlines()]
    assert events[:1] == ["eval"] and "decision" in events and "order_posted" in events and "fill" in events and events[-1] == "settled"


def test_loss_settlement_and_daily_stop(tmp_path):
    clock = Clock(TS + 120)
    eng, pm, ledger, _ = build(tmp_path, clock, daily_loss_limit_usd=3.0)
    pm.after_calls = (3, TU, (0.60, 0.61))
    assert eng.step(clock.now) == "filled"
    pm.completed = (STRIKE, STRIKE - 5.0)  # fechou abaixo: Down
    clock.now = TS + 330
    eng.settle_pending(clock.now)
    row = ledger.get(TS)
    assert row["outcome"] == "Down" and row["pnl_usd"] == pytest.approx(-8.19 * 0.61, abs=1e-3)
    # próxima janela: perda do dia ultrapassa o stop => pula
    clock.now = TS + 300 + 120
    pm.market_obj = Market5m(**{**pm.market_obj.__dict__, "ts_start": TS + 300, "ts_end": TS + 600})
    pm.__class__.market = lambda self, ts: self.market_obj
    assert eng.step(clock.now).startswith("skip:stop diário")


def test_no_edge_does_not_call_jev(tmp_path):
    clock = Clock(TS + 120)
    pm = FakePM()
    pm.books = {TU: (0.49, 0.50), TD: (0.49, 0.50)}
    eng, _, _, jev = build(tmp_path, clock, pm=pm, feed=make_feed(clock.now, delta=0.0))
    assert eng.step(clock.now) == "no_edge"
    assert jev._cls.calls == 0


def test_favored_side_only_blocks_cheap_tail(tmp_path):
    clock = Clock(TS + 120)
    pm = FakePM()
    pm.books = {TU: (0.11, 0.12), TD: (0.88, 0.89)}  # Up barato; modelo diz ~0,20 para Up
    eng, _, _, jev = build(tmp_path, clock, pm=pm, feed=make_feed(clock.now, delta=-45.0))
    assert eng.step(clock.now) == "no_edge_favored"
    assert jev._cls.calls == 0
    eng2, *_ = build(tmp_path / "b", clock, pm=pm, feed=make_feed(clock.now, delta=-45.0), favored_side_only=False)
    assert eng2.step(clock.now) in ("vetoed", "filled", "requote", "unfilled")


def test_anomaly_veto_blocks_order(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, jev = build(tmp_path, clock, jev=fake_jev(anomaly=0.9))
    assert eng.step(clock.now) == "vetoed"
    assert ledger.get(TS)["status"] == "seen"
    assert jev._cls.calls == 1
    # dentro do intervalo mínimo reaproveita o veredito, sem nova chamada
    clock.now += 10
    eng.feed.add(clock.now, STRIKE + 60)
    assert eng.step(clock.now) == "vetoed"
    assert jev._cls.calls == 1
    # passado o intervalo: segunda chamada cabe no orçamento (2), terceira não
    clock.now += 40
    eng.feed.add(clock.now, STRIKE + 60)
    assert eng.step(clock.now) == "vetoed"
    assert jev._cls.calls == 2
    clock.now += 50
    eng.feed.add(clock.now, STRIKE + 60)
    assert eng.step(clock.now).startswith("skip:orçamento")
    assert jev._cls.calls == 2
    assert ledger.is_final(TS)


def test_cancel_unconfirmed_closes_window_without_requote(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, max_requotes=3, order_ttl_s=5)

    class StuckBroker(PaperBroker):
        def cancel(self, order_id):
            return False  # CLOB não confirmou

    eng.broker = StuckBroker(pm.book, 25.0)
    assert eng.step(clock.now) == "cancel_unconfirmed"
    row = ledger.get(TS)
    assert row["status"] == "orphan" and "cancelamento" in row["reason"]
    assert eng.step(clock.now) == "done"  # janela fechada: nada de recotar com ordem possivelmente viva


def test_strike_change_before_order_aborts_placement(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock)
    first = eng.step(clock.now)  # cacheia strike, chama Jev, vai colocar ordem
    assert first in ("filled", "requote", "unfilled")
    eng2, pm2, ledger2, _ = build(tmp_path / "b", clock)
    original = pm2.price_to_beat
    calls = {"n": 0}

    def flaky(ts):
        calls["n"] += 1
        p = original(ts)
        if calls["n"] >= 2:  # a reconferência antes da ordem vê outro valor
            return PriceToBeat(p.open_price + 5.0, None, False)
        return p

    pm2.price_to_beat = flaky
    assert eng2.step(clock.now) == "strike_changed"
    assert ledger2.get(TS)["strike"] == pytest.approx(STRIKE + 5.0)
    assert ledger2.get(TS)["status"] == "seen"


def test_settlement_prefers_gamma_and_reconciles_local(tmp_path):
    clock = Clock(TS + 120)
    eng, pm, ledger, _ = build(tmp_path, clock)
    pm.after_calls = (3, TU, (0.60, 0.61))
    assert eng.step(clock.now) == "filled"
    # liquidação local (Gamma ainda sem resultado): Up
    pm.completed = (STRIKE, STRIKE + 1.0)
    clock.now = TS + 330
    assert eng.settle_pending(clock.now) == 1
    row = ledger.get(TS)
    assert row["outcome"] == "Up" and row["outcome_source"] == "local" and row["reconciled"] == 0
    # Gamma fecha dizendo Down: reconciliação corrige o PnL e registra alerta
    pm.gamma_outcome = lambda ts: "Down"
    clock.now = TS + 300 + 130
    assert eng.reconcile_settled(clock.now) == 1
    row = ledger.get(TS)
    assert row["outcome"] == "Down" and row["outcome_source"] == "gamma" and row["reconciled"] == 1
    assert row["pnl_usd"] == pytest.approx(-row["cost_usd"], abs=1e-6)
    assert "settlement_mismatch" in ledger.journal_path.read_text()


def test_direction_disagreement_vetoes(tmp_path):
    clock = Clock(TS + 120)
    eng, *_ = build(tmp_path, clock, jev=fake_jev(direction=0.3))  # modelo diz Up, Jev dá 0,3
    assert eng.step(clock.now) == "vetoed"


def test_ttl_requote_then_unfilled(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, max_requotes=2, order_ttl_s=5)
    assert eng.step(clock.now) == "requote"
    assert ledger.get(TS)["requotes"] == 1 and ledger.get(TS)["status"] == "seen"
    assert eng.step(clock.now) == "unfilled"
    assert ledger.is_final(TS)
    assert eng.step(clock.now) == "done"


def test_stale_feed_fails_closed(tmp_path):
    clock = Clock(TS + 120)
    eng, _, ledger, jev = build(tmp_path, clock, feed=make_feed(clock.now - 30))
    assert eng.step(clock.now).startswith("skip:feed chainlink velho")
    assert jev._cls.calls == 0 and ledger.get(TS)["status"] == "seen"


def test_missing_strike_or_market_fails_closed(tmp_path):
    clock = Clock(TS + 120)
    pm = FakePM()
    pm.strike = None
    eng, _, _, jev = build(tmp_path, clock, pm=pm)
    assert eng.step(clock.now) == "skip:price_to_beat indisponível"
    pm2 = FakePM()
    pm2.market_obj = None
    eng2, *_ = build(tmp_path / "b", clock, pm=pm2)
    assert eng2.step(clock.now) == "skip:mercado não encontrado"
    assert jev._cls.calls == 0


def test_kill_switch(tmp_path):
    clock = Clock(TS + 120)
    eng, *_ = build(tmp_path, clock)
    (Path(tmp_path) / "KILL").write_text("x")
    assert eng.step(clock.now) == "skip:kill_switch"


def test_insufficient_funds_is_final_skip(tmp_path):
    clock = Clock(TS + 120)
    eng, pm, ledger, _ = build(tmp_path, clock, paper_bankroll_usd=1.53)
    assert eng.step(clock.now).startswith("skip:saldo insuficiente")
    assert ledger.is_final(TS)

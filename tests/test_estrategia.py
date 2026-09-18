"""Evoluções sob bandeira (desligadas no live por default): entrada taker, saída antecipada,
sizing por convicção, prior de σ da série real e failover de egress."""
import json

import pytest

from src.egress import EgressMonitor
from src.execution_5m import PaperBroker
from src.model import Candidate, entry_for, sigma_prior_from_windows, stake_for
from tests.test_engine import STRIKE, TD, TS, TU, Clock, build, make_feed
from tests.test_evolucao import Spy, events


def cand(edge_maker=0.02, edge_taker=0.0, p_side=0.62, ask=0.60):
    return Candidate(side="Up", token_id=TU, limit_price=p_side - edge_maker, p_side=p_side,
                     edge_maker=edge_maker, edge_taker=edge_taker, best_bid=ask - 0.01, best_ask=ask)


# ------------------------------------------------------------------ portão de entrada
def test_entry_compares_expected_value_not_nominal_edge():
    """O limite maker nunca passa do ask, então edge_maker > edge_taker SEMPRE. Comparar os dois nominais
    tornava o caminho taker inalcançável; o que vale é fill x edge."""
    assert entry_for(cand(edge_maker=0.06), 0.04, False, 0.10).kind == "maker"
    assert entry_for(cand(edge_maker=0.02, edge_taker=0.15), 0.04, False, 0.10) is None   # taker proibido
    assert entry_for(cand(edge_maker=0.02, edge_taker=0.09), 0.04, True, 0.10) is None    # nem taker nem maker

    c = cand(edge_maker=0.13, edge_taker=0.11)
    assert entry_for(c, 0.04, True, 0.10, maker_fill_rate=1.0).kind == "maker"   # se o maker sempre executa
    assert entry_for(c, 0.04, True, 0.10, maker_fill_rate=0.5).kind == "taker"   # com 50% de fill, taker ganha
    e = entry_for(c, 0.04, True, 0.10, maker_fill_rate=0.5)
    assert e.price == 0.60 and e.edge == 0.11
    # edge taker abaixo do mínimo: continua maker mesmo com fill ruim
    assert entry_for(cand(edge_maker=0.13, edge_taker=0.05), 0.04, True, 0.10, maker_fill_rate=0.3).kind == "maker"


def test_taker_entry_fills_immediately_and_pays_the_ask(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, min_net_edge=0.9, allow_taker=True, taker_min_edge=0.01)
    assert eng.step(clock.now) == "filled"                    # maker fora do mínimo; taker come o ask
    row = ledger.get(TS)
    assert row["fill_price"] == pytest.approx(0.62) and row["filled_shares"] > 0
    posted = [e for e in events(ledger) if e["event"] == "order_posted"]
    assert posted[0]["kind"] == "taker"
    assert not any(o.open for o in eng.broker.orders.values())   # FOK nunca deixa ordem no book


def test_taker_does_not_pay_an_ask_that_ran_away_before_the_post(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, min_net_edge=0.9, allow_taker=True, taker_min_edge=0.01, max_requotes=1)
    pm.after_calls = (2, TU, (0.60, 0.95))                    # o ask foge entre a decisão e o post
    assert eng.step(clock.now) == "edge_gone_at_post"
    assert not eng.broker.orders and not ledger.is_final(TS)


def test_taker_killed_at_execution_leaves_nothing_resting(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, min_net_edge=0.9, allow_taker=True, taker_min_edge=0.01, max_requotes=1)
    pm.after_calls = (3, TU, (0.60, 0.95))                    # o ask some entre o post e a execução
    assert eng.step(clock.now) == "unfilled"
    assert all(not o.open for o in eng.broker.orders.values())
    assert [e["event"] for e in events(ledger) if e["event"] in ("fill", "taker_killed")] == ["taker_killed"]


def test_live_keeps_taker_off_by_default(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, jev = build(tmp_path, clock, min_net_edge=0.9)
    assert eng.step(clock.now) == "no_edge" and jev._cls.calls == 0


# ------------------------------------------------------------------ sizing
def test_conviction_sizing_scales_between_floor_and_cap():
    assert stake_for(0.05, "fixed", 1.0, 5.0) == 5.0
    assert stake_for(0.00, "conviction", 1.0, 5.0) == 1.0
    assert stake_for(0.06, "conviction", 1.0, 5.0, ref_edge=0.12) == 3.0
    assert stake_for(0.30, "conviction", 1.0, 5.0, ref_edge=0.12) == 5.0   # edge enorme não passa do teto


def test_engine_sizes_by_conviction(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, sizing_mode="conviction", min_stake_usd=1.0, max_stake_usd=5.0,
                               min_shares=1.0, min_notional_usd=0.5, max_requotes=1)
    assert eng.step(clock.now) == "unfilled"
    posted = [e for e in events(ledger) if e["event"] == "order_posted"]
    stake = posted[0]["shares"] * posted[0]["limit"]
    assert 1.0 <= stake < 5.0                                  # edge ~0,05: aposta no meio da faixa


# ------------------------------------------------------------------ saída antecipada
def _fill_position(tmp_path, clock, **over):
    eng, pm, ledger, jev = build(tmp_path, clock, **over)
    pm.after_calls = (4, TU, (0.60, 0.61))
    assert eng.step(clock.now) == "filled"
    return eng, pm, ledger


def test_early_exit_sells_when_the_model_turns_against_the_position(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger = _fill_position(tmp_path, clock, early_exit_p=0.35, mark_interval_s=0)
    eng.notifier = spy = Spy()
    cost = ledger.get(TS)["cost_usd"]
    clock.now += 40
    pm.after_calls = (0, TU, (0.30, 0.32))                     # book do Up desabou junto
    eng.feed = make_feed(clock.now, delta=-200.0)              # preço bem abaixo do strike: p do Up despenca
    assert eng.step(clock.now) == "closed"
    row = ledger.get(TS)
    assert row["status"] == "closed" and row["pnl_usd"] < 0 and row["outcome_source"] == "early_exit"
    proceeds = 8.19 * 0.30 - 0.07 * 0.30 * 0.70 * 8.19
    assert row["pnl_usd"] == pytest.approx(proceeds - cost, abs=1e-2)
    assert ledger.realized_pnl_usd() == pytest.approx(row["pnl_usd"])   # conta como PnL do dia
    assert spy.keys() == ["exit"]
    assert eng.step(clock.now) == "done" and eng.settle_pending(TS + 400) == 0   # não liquida de novo


def test_early_exit_is_off_by_default_and_respects_its_guards(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger = _fill_position(tmp_path, clock, mark_interval_s=0)
    clock.now += 40
    pm.after_calls = (0, TU, (0.30, 0.32))
    eng.feed = make_feed(clock.now, delta=-200.0)
    assert eng.step(clock.now) == "holding" and ledger.get(TS)["status"] == "filled"   # flag desligada

    clock2 = Clock(TS + 60)
    eng2, pm2, ledger2 = _fill_position(tmp_path / "b", clock2, early_exit_p=0.35, mark_interval_s=0,
                                        early_exit_min_proceeds_usd=99.0)
    clock2.now += 40
    pm2.after_calls = (0, TU, (0.30, 0.32))
    eng2.feed = make_feed(clock2.now, delta=-200.0)
    assert eng2.step(clock2.now) == "holding"                  # troco menor que o mínimo: não vende

    clock3 = Clock(TS + 60)
    eng3, pm3, ledger3 = _fill_position(tmp_path / "c", clock3, early_exit_p=0.35, mark_interval_s=0)
    clock3.now = TS + 280                                      # fim da janela: vender não adianta mais
    pm3.after_calls = (0, TU, (0.30, 0.32))
    eng3.feed = make_feed(clock3.now, delta=-200.0)
    assert eng3.step(clock3.now) == "outside"


def test_early_exit_without_a_buyer_keeps_the_position(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger = _fill_position(tmp_path, clock, early_exit_p=0.35, mark_interval_s=0)
    clock.now += 40
    eng.feed = make_feed(clock.now, delta=-200.0)

    class NoBidBroker(PaperBroker):
        def sell_taker(self, token_id, price, size):
            st = super().sell_taker(token_id, price, size)
            st.filled, st.status = 0.0, "KILLED"               # o bid sumiu no meio
            return st

    pm.after_calls = (0, TU, (0.30, 0.32))
    eng.broker = NoBidBroker(pm.book, 25.0)
    assert eng.step(clock.now) == "holding"
    assert ledger.get(TS)["status"] == "filled"
    assert [e["event"] for e in events(ledger)][-1] == "exit_killed"


# ------------------------------------------------------------------ prior de σ
def test_sigma_prior_learns_from_the_real_series_within_bounds():
    rows = [{"strike": 76000.0, "close_price": 76000.0 * (1 + 0.002 * (1 if i % 2 else -1))} for i in range(120)]
    assert sigma_prior_from_windows(rows[:50], 5.7e-5, 100, 0.5) is None          # amostra curta
    learned = sigma_prior_from_windows(rows, 5.7e-5, 100, 0.5)
    assert learned == pytest.approx(5.7e-5 * 1.5)                                  # série agitada: trava no teto
    calm = [{"strike": 76000.0, "close_price": 76000.0 * (1 + 1e-9)} for _ in range(120)]
    assert sigma_prior_from_windows(calm, 5.7e-5, 100, 0.5) == pytest.approx(5.7e-5 * 0.5)


def test_engine_uses_the_learned_prior_only_when_enabled(tmp_path):
    clock = Clock(TS + 60)
    eng, _, ledger, _ = build(tmp_path, clock, sigma_prior_auto=True, sigma_prior_min_windows=2)
    for i, close in enumerate((76000.0 * 1.002, 76000.0 * 0.998, 76000.0 * 1.002)):
        ledger.upsert(TS - 300 * (i + 1), status="settled", strike=76000.0, close_price=close)
    assert eng._prior_1s(clock.now) == pytest.approx(eng.s.sigma_prior_1s * 1.5)
    assert any(e["event"] == "sigma_prior" for e in events(ledger))

    eng2, _, ledger2, _ = build(tmp_path / "b", Clock(TS + 60))
    ledger2.upsert(TS - 300, status="settled", strike=76000.0, close_price=76000.0 * 1.002)
    assert eng2._prior_1s(TS + 60) == eng2.s.sigma_prior_1s                        # desligado: constante


# ------------------------------------------------------------------ failover de egress
def test_egress_failover_picks_the_next_allowed_route():
    clock, spy = Clock(0), Spy()
    answers = {"socks5://tor": ("1.1.1.1", "CH"), "socks5://vpn": ("2.2.2.2", "SE")}
    mon = EgressMonitor("socks5://tor", interval_s=300, notifier=spy, fallbacks=["socks5://vpn"],
                        probe_fn=lambda s: answers[s], clock=clock, region_hold_s=300)
    assert mon.check().ok and mon.desired_proxy() == "socks5://tor" and spy.sent == []

    answers["socks5://tor"] = None                              # Tor caiu
    st = mon.check()
    assert st.ok and mon.desired_proxy() == "socks5://vpn" and spy.keys() == ["egress_switch"]

    answers["socks5://tor"] = ("1.1.1.1", "CH")                 # Tor voltou: não troca à toa
    assert mon.check().ok and mon.desired_proxy() == "socks5://vpn"

    mon.mark_region_block()                                     # CLOB recusou a VPN por região
    assert not mon.ok()
    assert mon.check().ok and mon.desired_proxy() == "socks5://tor"


def test_egress_fails_closed_when_every_route_is_blocked():
    clock = Clock(0)
    mon = EgressMonitor(["socks5://a", "socks5://b"], notifier=Spy(),
                        probe_fn=lambda s: ("1.1.1.1", "BR" if s.endswith("a") else "US"), clock=clock)
    assert not mon.check().ok and not mon.ok()


def test_engine_applies_the_route_before_posting(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, max_requotes=1)
    applied = []

    class RoutedBroker(PaperBroker):
        def set_proxy(self, socks):
            applied.append(socks)
            return True

    eng.broker = RoutedBroker(pm.book, 25.0)
    eng.egress = EgressMonitor("socks5://vpn", probe_fn=lambda s: ("2.2.2.2", "SE"), clock=clock)
    eng.egress.check()
    assert eng.step(clock.now) == "unfilled"
    assert applied == ["socks5://vpn"]
    assert [e for e in events(ledger) if e["event"] == "egress_applied"]


def test_route_is_applied_even_with_nothing_to_trade(tmp_path):
    """A rota precisa valer também para a varredura de ordens antigas, não só para postar."""
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, min_net_edge=0.9)
    applied = []

    class RoutedBroker(PaperBroker):
        def set_proxy(self, socks):
            applied.append(socks)
            return True

    eng.broker = RoutedBroker(pm.book, 25.0)
    eng.egress = EgressMonitor("socks5://vpn", probe_fn=lambda s: ("2.2.2.2", "SE"), clock=clock)
    eng.egress.check()
    assert eng.step(clock.now) == "no_edge" and applied == ["socks5://vpn"]


# ------------------------------------------------------------------ achados da 3ª revisão cruzada
def test_early_exit_does_not_poison_the_btc_series(tmp_path):
    """close_price é preço de BTC. Gravar ali o preço do token (0,30) faria log(0,30/76000) explodir a
    σ da série inteira e o prior aprendido ficaria travado no teto."""
    clock = Clock(TS + 60)
    eng, pm, ledger = _fill_position(tmp_path, clock, early_exit_p=0.35, mark_interval_s=0,
                                     sigma_prior_auto=True, sigma_prior_min_windows=1)
    clock.now += 40
    pm.after_calls = (0, TU, (0.30, 0.32))
    eng.feed = make_feed(clock.now, delta=-200.0)
    assert eng.step(clock.now) == "closed"
    row = ledger.get(TS)
    assert row["exit_price"] == pytest.approx(0.30) and row["close_price"] is None
    eng._prior_checked_at = 0
    assert eng._prior_1s(clock.now + 7200) == pytest.approx(eng.s.sigma_prior_1s)   # série ainda sã

    from src.model import sigma_prior_from_windows
    poison = [{"strike": 76000.0, "close_price": 0.30}] + [{"strike": 76000.0, "close_price": 76010.0}] * 120
    assert sigma_prior_from_windows(poison, 5.7e-5, 100, 0.5) == pytest.approx(
        sigma_prior_from_windows(poison[1:], 5.7e-5, 100, 0.5))


def test_partial_exit_books_the_cash_and_counts_for_the_daily_stop(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger = _fill_position(tmp_path, clock, early_exit_p=0.35, mark_interval_s=0, daily_loss_limit_usd=2.0)
    shares = ledger.get(TS)["filled_shares"]
    cost = ledger.get(TS)["cost_usd"]

    class HalfSellBroker(PaperBroker):
        def sell_taker(self, token_id, price, size):
            st = super().sell_taker(token_id, price, size)
            st.filled = size / 2                                  # só metade encontrou comprador
            return st

    clock.now += 40
    pm.after_calls = (0, TU, (0.30, 0.32))
    eng.feed = make_feed(clock.now, delta=-200.0)
    eng.broker = HalfSellBroker(pm.book, 25.0)
    assert eng.step(clock.now) == "holding"
    row = ledger.get(TS)
    assert row["filled_shares"] == pytest.approx(shares / 2) and row["cost_usd"] == pytest.approx(cost / 2, abs=1e-3)
    partial = row["partial_pnl_usd"]
    assert partial < 0 and ledger.realized_pnl_usd() == pytest.approx(partial)   # entra no PnL do dia
    from src.ledger import day_of
    assert ledger.realized_pnl_usd(day_of(TS)) == pytest.approx(partial)


def test_unconfirmed_sell_is_never_repeated_and_is_resolved_later(tmp_path):
    from src.execution_5m import OrderState

    clock = Clock(TS + 60)
    eng, pm, ledger = _fill_position(tmp_path, clock, early_exit_p=0.35, mark_interval_s=0)
    eng.notifier = spy = Spy()
    shares = ledger.get(TS)["filled_shares"]
    sells = {"n": 0, "qty": 0.0}

    class LimboSellBroker(PaperBroker):
        def sell_taker(self, token_id, price, size):
            sells["n"] += 1
            return OrderState("sell-1", token_id, price, size, filled=0.0, open=True, status="UNKNOWN")

        def resolve(self, order_id, since_ts):
            return OrderState(order_id, TU, 0.30, shares, filled=sells["qty"], avg_price=0.30,
                              open=False, status="GONE" if sells["qty"] else "KILLED")

    clock.now += 40
    pm.after_calls = (0, TU, (0.30, 0.32))
    eng.feed = make_feed(clock.now, delta=-200.0)
    eng.broker = LimboSellBroker(pm.book, 25.0)
    assert eng.step(clock.now) == "holding"
    assert ledger.get(TS)["exit_pending"] == 1 and spy.keys() == ["exit_unconfirmed"]
    clock.now += 5
    eng.feed = make_feed(clock.now, delta=-200.0)                      # feed fresco: o passo chega ao _mark
    assert eng.step(clock.now) == "holding" and sells["n"] == 1        # e ainda assim não vende de novo

    sells["qty"] = shares                                              # a venda tinha executado
    assert eng.resolve_pending_exits(clock.now) == 1
    row = ledger.get(TS)
    assert row["status"] == "closed" and row["exit_pending"] == 0 and row["pnl_usd"] < 0


def test_unconfirmed_sell_that_died_releases_the_window(tmp_path):
    from src.execution_5m import OrderState

    clock = Clock(TS + 60)
    eng, pm, ledger = _fill_position(tmp_path, clock, early_exit_p=0.35, mark_interval_s=0)

    class DeadSellBroker(PaperBroker):
        def sell_taker(self, token_id, price, size):
            return OrderState("sell-x", token_id, price, size, filled=0.0, open=True, status="")

        def resolve(self, order_id, since_ts):
            return OrderState(order_id, TU, 0.30, 0.0, filled=0.0, open=False, status="KILLED")

    clock.now += 40
    pm.after_calls = (0, TU, (0.30, 0.32))
    eng.feed = make_feed(clock.now, delta=-200.0)
    eng.broker = DeadSellBroker(pm.book, 25.0)
    eng.step(clock.now)
    assert ledger.get(TS)["exit_pending"] == 1
    assert eng.resolve_pending_exits(clock.now) == 1
    assert ledger.get(TS)["exit_pending"] == 0 and ledger.get(TS)["status"] == "filled"


def test_taker_without_a_confirmed_death_does_not_buy_again(tmp_path):
    """FOK cuja resposta não diz o que aconteceu e cujos trades ainda não indexaram: a janela fecha como
    órfã em vez de liberar uma segunda compra."""
    from src.execution_5m import OrderState

    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, min_net_edge=0.9, allow_taker=True, taker_min_edge=0.01,
                               max_requotes=3, requote_recheck_s=0.1)
    seen = {"n": 0}

    class LaggingTakerBroker(PaperBroker):
        def place_taker(self, token_id, price, size):
            seen["n"] += 1
            return OrderState(f"tk-{seen['n']}", token_id, price, size, filled=0.0, open=False, status="GONE")

        def resolve(self, order_id, since_ts):
            return OrderState(order_id, TU, 0.62, 8.0, filled=0.0, open=False, status="GONE")

    eng.broker = LaggingTakerBroker(pm.book, 25.0)
    assert eng.step(clock.now) == "cancel_unconfirmed"
    assert ledger.get(TS)["status"] == "orphan"
    assert eng.step(clock.now) == "done" and seen["n"] == 1


def test_proxy_credentials_are_never_logged():
    from src.egress import EgressMonitor, redact

    assert redact("socks5://bruno:s3nha@vpn.example:1080") == "socks5://***@vpn.example:1080"
    assert redact(None) == "DIRETO" and redact("socks5://127.0.0.1:9050") == "socks5://127.0.0.1:9050"
    clock, spy = Clock(0), Spy()
    mon = EgressMonitor("socks5://tor", notifier=spy, fallbacks=["socks5://u:p@vpn:1080"],
                        probe_fn=lambda s: None if s == "socks5://tor" else ("2.2.2.2", "SE"), clock=clock)
    mon.check()
    assert spy.keys() == ["egress_switch"] and "p@vpn" not in spy.sent[0][1] and "***@vpn" in spy.sent[0][1]


# ------------------------------------------------------------------ portões do Jev separáveis
def test_jev_gate_off_keeps_the_regime_adjustment(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, jev = build(tmp_path, clock, jev_gate=False, max_requotes=1)
    eng.jev = jev = __import__("tests.test_engine", fromlist=["fake_jev"]).fake_jev(direction=0.05)  # Jev contra o Up
    assert eng.step(clock.now) == "unfilled"                   # sem portão, o veto de direção não corta
    d = [e for e in events(ledger) if e["event"] == "decision"][0]
    assert d["vetoed"] is None and d["sigma_mult"] != 1.0 and jev._cls.calls == 1


def test_both_jev_flags_off_skips_the_call_entirely(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, jev = build(tmp_path, clock, jev_gate=False, jev_regime_adjust=False, max_requotes=1)
    assert eng.step(clock.now) == "unfilled"
    d = [e for e in events(ledger) if e["event"] == "decision"][0]
    assert jev._cls.calls == 0                                  # não paga latência nem chamada
    assert d["jev"] is None and d["sigma_mult"] == 1.0 and d["p_adj"] == d["p_raw"]


def test_jev_gate_on_still_vetoes(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock)
    eng.jev = __import__("tests.test_engine", fromlist=["fake_jev"]).fake_jev(direction=0.05)
    assert eng.step(clock.now) == "vetoed"


# ------------------------------------------------------------------ Jev como meta-julgamento e como tamanho
def fake_jev_meta(reliability=0.9, anomaly=0.1, probs=None):
    """Conjunto 'meta': sem pergunta de direção, com confiabilidade da estimativa do modelo."""
    from types import SimpleNamespace

    from src.jev_5m import JevGate

    class C:
        calls = 0
        last_state = None

        def system_one(self, state, questions):
            C.calls += 1
            C.last_state = state
            assert "direction_up" not in questions and "reliability" in questions
            return SimpleNamespace(
                scores={"regime": SimpleNamespace(score=1.0, probabilities=probs or {0: .2, 1: .5, 2: .3}, confidence=0.6)},
                nouls={"anomaly": SimpleNamespace(noul=anomaly), "reliability": SimpleNamespace(noul=reliability)},
            )

    gate = JevGate(client_factory=lambda: C(), question_set="meta")
    gate._cls = C
    return gate


def test_meta_question_set_sends_the_model_estimate_and_drops_direction(tmp_path):
    clock = Clock(TS + 60)
    jev = fake_jev_meta()
    eng, pm, ledger, _ = build(tmp_path, clock, jev=jev, jev_question_set="meta", max_requotes=1)
    assert eng.step(clock.now) == "unfilled"
    st = jev._cls.last_state
    assert 0 < st["model_p_up"] < 1                                  # a pergunta meta precisa do p do modelo
    d = [e for e in events(ledger) if e["event"] == "decision"][0]
    assert d["jev"]["reliability_p"] == 0.9 and d["jev"]["direction_p_up"] is None
    assert d["vetoed"] is None                                        # sem pergunta de direção, sem veto de lado


def test_meta_veto_uses_reliability(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, jev=fake_jev_meta(reliability=0.2),
                               jev_question_set="meta", jev_min_reliability=0.5)
    assert eng.step(clock.now) == "vetoed"
    assert "confiança" in [e for e in events(ledger) if e["event"] == "decision"][0]["vetoed"]


def test_sizing_by_jev_reliability(tmp_path):
    from src.model import stake_for

    assert stake_for(0.12, "jev", 1.0, 5.0, reliability=1.0) == 5.0
    assert stake_for(0.12, "jev", 1.0, 5.0, reliability=0.5) == 3.0
    assert stake_for(0.12, "jev", 1.0, 5.0, reliability=None) == 1.0   # sem julgamento, aposta o piso
    assert stake_for(0.06, "jev", 1.0, 5.0, reliability=1.0) == 3.0

    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, jev=fake_jev_meta(reliability=0.25), jev_question_set="meta",
                               sizing_mode="jev", min_stake_usd=1.0, max_stake_usd=5.0, min_shares=1.0,
                               min_notional_usd=0.5, max_requotes=1)
    assert eng.step(clock.now) == "unfilled"
    posted = [e for e in events(ledger) if e["event"] == "order_posted"][0]
    # Confiança baixa pede aposta pequena, mas o mercado não vende menos de 5 shares: o motor sobe ao
    # piso em vez de perder a janela, e registra que subiu.
    assert posted["reliability"] == 0.25 and posted["stake"] < 5.0
    assert posted["shares"] == pytest.approx(5.0) and any(e["event"] == "stake_raised_to_minimum" for e in events(ledger))


def test_stake_below_the_market_minimum_is_skipped_when_it_does_not_fit(tmp_path):
    """5 shares a 0,61 custam 3,05. Com teto de 2 não existe ordem possível: a janela fecha com o motivo
    certo, em vez de 'saldo insuficiente' com saldo sobrando."""
    clock2 = Clock(TS + 60)
    eng2, _, ledger2, _ = build(tmp_path / "b", clock2, sizing_mode="conviction", min_stake_usd=1.0, max_stake_usd=2.0)
    assert eng2.step(clock2.now).startswith("skip:mínimo do mercado acima do teto")
    assert ledger2.is_final(TS)


def test_sizing_by_jev_requires_the_meta_question_set():
    from src.config import Settings

    with pytest.raises(ValueError, match="SIZING_MODE=jev"):
        Settings.from_env({"SIZING_MODE": "jev"})
    s = Settings.from_env({"SIZING_MODE": "jev", "JEV_QUESTION_SET": "meta"})
    assert s.sizing_mode == "jev" and s.jev_question_set == "meta"

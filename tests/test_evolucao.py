"""Observação, robustez e execução: alertas, ordens sem destino confirmado, rejeição por book
cruzado, portão de egress, backfill de resultados, marcação da posição e resumo diário."""
import json

import pytest

from src.egress import EgressMonitor, classify
from src.execution_5m import OrderState, PaperBroker, order_error_kind
from src.notify import Notifier
from tests.test_engine import STRIKE, TD, TS, TU, Clock, build


class Spy:
    def __init__(self):
        self.sent = []

    def send(self, key, text, min_interval_s=None, mono=None):
        self.sent.append((key, text if mono is None else f"{text}\n{mono}"))
        return True

    def keys(self):
        return [k.split(":")[0] for k, _ in self.sent]


def events(ledger):
    return [json.loads(l) for l in ledger.journal_path.read_text().splitlines()]


# ------------------------------------------------------------------ notificador
def test_notifier_dedupes_by_key_and_never_raises():
    clock = Clock(1000)
    posted = []

    def post(url, payload):
        posted.append(payload["text"])
        raise RuntimeError("telegram fora")

    n = Notifier("T", "C", prefix="[x] ", min_interval_s=600, post=post, clock=clock, start=False)
    assert n.send("k", "um") is True
    assert n.send("k", "dois") is False          # mesma chave dentro do intervalo
    assert n.send("outra", "três") is True
    clock.now += 601
    assert n.send("k", "quatro") is True
    while n.drain_once():                         # falha de envio não propaga
        pass
    assert posted == ["[x] um", "[x] três", "[x] quatro"]


# ------------------------------------------------------------------ egress
def test_egress_classification_uses_polymarket_no_open_list():
    assert classify(("1.1.1.1", "CH"), 0).ok
    for cc in ("DE", "BR", "US", "FR", "CA"):     # DE era a saída Tor dos 403 de 18/09
        assert not classify(("1.1.1.1", cc), 0).ok
    assert not classify(None, 0).ok               # proxy caído = falha fechada


def test_egress_monitor_alerts_on_transitions_and_expires():
    clock, spy, answers = Clock(0), Spy(), [("1.1.1.1", "CH"), None, ("2.2.2.2", "SE")]
    mon = EgressMonitor("socks5://x", interval_s=300, notifier=spy, probe_fn=lambda s: answers.pop(0), clock=clock)
    assert not mon.ok()                            # nunca verificado
    assert mon.check().ok and mon.ok() and spy.sent == []
    assert not mon.check().ok and not mon.ok()
    assert mon.check().ok
    assert spy.keys() == ["egress_down", "egress_up"]
    clock.now += 3 * 300 + 1
    assert not mon.ok()                            # verificação vencida não vale
    mon.mark_region_block()
    assert not mon.ok() and "403" in mon.reason()


def test_engine_does_not_post_when_egress_is_down(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, jev = build(tmp_path, clock)
    eng.egress = EgressMonitor("socks5://x", probe_fn=lambda s: ("1.1.1.1", "DE"), clock=clock)
    eng.egress.check()
    assert eng.step(clock.now).startswith("skip:egress")
    assert jev._cls.calls == 0 and not ledger.is_final(TS)   # não gasta Jev e volta sozinho
    eng.egress = EgressMonitor("socks5://x", probe_fn=lambda s: ("1.1.1.1", "CH"), clock=clock)
    eng.egress.check()
    assert eng.step(clock.now) == "requote"


# ------------------------------------------------------------------ execução
def test_order_error_kinds():
    assert order_error_kind(RuntimeError("PolyApiException[status_code=400, error_message={'error': 'invalid post-only order: order crosses book'}]")) == "cross"
    assert order_error_kind(RuntimeError("PolyApiException[status_code=403, error_message={'error': 'Trading restricted in your region, please refer'}]")) == "region"
    assert order_error_kind(RuntimeError("PolyApiException[status_code=400, error_message={'error': 'not enough balance'}]")) == "rejected"
    assert order_error_kind(RuntimeError("PolyApiException[status_code=None, error_message=Request exception!]")) == "unknown"
    assert order_error_kind(TimeoutError("x")) == "unknown"


class FailingBroker(PaperBroker):
    def __init__(self, book_fn, exc, fail_times=99):
        super().__init__(book_fn, 25.0)
        self.exc, self.fail_times, self.attempts = exc, fail_times, 0

    def place_maker(self, token_id, price, size):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise self.exc
        return super().place_maker(token_id, price, size)


def test_cross_rejection_does_not_burn_requotes(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, max_requotes=1, max_cross_retries=3)
    eng.notifier = spy = Spy()
    eng.broker = FailingBroker(pm.book, RuntimeError("invalid post-only order: order crosses book"), fail_times=2)
    assert eng.step(clock.now) == "cross_retry"
    assert eng.step(clock.now) == "cross_retry"
    assert (ledger.get(TS).get("requotes") or 0) == 0
    assert eng.step(clock.now) == "unfilled"       # 3ª tentativa posta; a única recotação foi de verdade
    assert ledger.get(TS)["requotes"] == 1
    assert "order_error" not in spy.keys()         # book cruzado é rotina, não alerta


def test_cross_rejection_budget_closes_window(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, max_cross_retries=2)
    eng.broker = FailingBroker(pm.book, RuntimeError("order crosses book"))
    assert eng.step(clock.now) == "cross_retry"
    assert eng.step(clock.now).startswith("skip:post-only")
    assert ledger.is_final(TS)


def test_region_403_closes_egress_without_burning_requote(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock)
    spy = Spy()
    eng.egress = EgressMonitor("socks5://x", notifier=spy, probe_fn=lambda s: ("1.1.1.1", "CH"), clock=clock)
    eng.egress.check()
    eng.broker = FailingBroker(pm.book, RuntimeError("status_code=403 Trading restricted in your region"))
    assert eng.step(clock.now) == "order_error_region"
    assert not eng.egress.ok() and spy.keys() == ["egress_down"]
    assert (ledger.get(TS).get("requotes") or 0) == 0
    assert eng.step(clock.now).startswith("skip:egress")


def test_clob_rejection_alerts_and_counts_as_requote(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock)
    eng.notifier = spy = Spy()
    eng.broker = FailingBroker(pm.book, RuntimeError("PolyApiException[status_code=400, error_message={'error': 'not enough balance'}]"))
    assert eng.step(clock.now) == "order_error"
    assert ledger.get(TS)["requotes"] == 1 and spy.keys() == ["order_error"]


def test_post_without_answer_sweeps_account_and_never_posts_again(tmp_path):
    """Resposta do POST perdida no proxy: a ordem pode estar viva sem order_id. Antes disto o motor
    postava outra a cada 2 s (até 3 ordens vivas, US$ 15 contra o teto de US$ 5)."""
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock)
    eng.notifier = spy = Spy()

    class LostAnswerBroker(PaperBroker):
        swept = 0

        def place_maker(self, token_id, price, size):
            super().place_maker(token_id, price, size)          # o CLOB aceitou...
            raise RuntimeError("PolyApiException[status_code=None, error_message=Request exception!]")  # ...e a resposta se perdeu

        def cancel_all(self):
            LostAnswerBroker.swept += 1
            super().cancel_all()

    eng.broker = broker = LostAnswerBroker(pm.book, 25.0)
    assert eng.step(clock.now).startswith("skip:post sem resposta")
    assert broker.swept == 1 and not any(o.open for o in broker.orders.values())
    assert ledger.is_final(TS) and eng.step(clock.now) == "done" and len(broker.orders) == 1
    assert spy.keys() == ["order_error"] and "cancel_all enviado" in spy.sent[0][1]


def test_stale_book_is_refreshed_before_posting(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, max_requotes=1)
    pm.after_calls = (2, TU, (0.62, 0.64))         # book do Up andou entre a decisão e o post
    assert eng.step(clock.now) == "unfilled"
    posted = [e for e in events(ledger) if e["event"] == "order_posted"]
    assert posted[0]["limit"] == pytest.approx(0.63) and "post_ms" in posted[0]


def test_no_post_when_refreshed_book_kills_the_edge(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock)
    pm.after_calls = (2, TU, (0.97, 0.99))
    assert eng.step(clock.now) == "edge_gone_at_post"
    assert not [e for e in events(ledger) if e["event"] == "order_posted"]


# ------------------------------------------------------------------ ordens sem destino confirmado
class FlakyPollBroker(PaperBroker):
    """poll levanta (rede) até `heal`; a ordem executa por baixo dos panos se `fills`."""

    def __init__(self, book_fn, fills):
        super().__init__(book_fn, 25.0)
        self.fills, self.healed, self.cancel_ok = fills, False, False

    def poll(self, order_id):
        if not self.healed:
            raise TimeoutError("proxy mudo")
        st = self.orders[order_id]
        if self.fills and st.filled == 0:
            st.filled, st.avg_price, st.open, st.status = st.size, st.price, False, "MATCHED"
        return super().poll(order_id)

    def cancel(self, order_id):
        return super().cancel(order_id) if self.healed else False


def test_poll_failure_never_leads_to_second_order(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, order_ttl_s=5)
    eng.notifier = spy = Spy()
    eng.broker = broker = FlakyPollBroker(pm.book, fills=True)
    assert eng.step(clock.now) == "cancel_unconfirmed"
    assert ledger.get(TS)["status"] == "orphan" and "cancel_unconfirmed" in spy.keys()
    assert eng.step(clock.now) == "done" and len(broker.orders) == 1

    broker.healed = True                           # rede volta: o fill escondido aparece no ledger
    assert eng.resolve_open_orders(clock.now) == 1
    row = ledger.get(TS)
    assert row["status"] == "filled" and row["filled_shares"] > 0
    assert [e for e in events(ledger) if e["event"] == "fill"][0]["recovered"] is True
    assert "fill" in spy.keys()


def test_orphan_without_fill_ends_unfilled(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, order_ttl_s=5)
    eng.broker = broker = FlakyPollBroker(pm.book, fills=False)
    assert eng.step(clock.now) == "cancel_unconfirmed"
    assert eng.resolve_open_orders(clock.now) == 0          # ainda mudo: segue pendente
    broker.healed = True
    assert eng.resolve_open_orders(clock.now) == 1
    assert ledger.get(TS)["status"] == "unfilled"


def test_restart_mid_quote_resolves_order_before_any_new_decision(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock)
    oid = eng.broker.place_maker(TU, 0.61, 8.19)
    ledger.upsert(TS, status="quoting", side="Up", limit_price=0.61, shares=8.19, order_id=oid, requotes=1)
    eng.broker.orders[oid] = OrderState(oid, TU, 0.61, 8.19, filled=8.19, avg_price=0.61, open=False, status="MATCHED")
    assert eng.step(clock.now) == "holding"                 # a varredura reconhece o fill em vez de postar outra
    assert len(eng.broker.orders) == 1 and ledger.get(TS)["status"] == "filled"
    assert ledger.get(TS)["filled_shares"] == pytest.approx(8.19)


def test_restart_mid_quote_in_paper_releases_window(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, max_requotes=3)
    ledger.upsert(TS, status="quoting", side="Up", limit_price=0.61, shares=8.19, order_id="paper-morta", requotes=1)
    assert eng.step(clock.now) == "requote"                 # ordem do processo antigo não existe mais
    assert ledger.get(TS)["requotes"] == 2


# ------------------------------------------------------------------ evidência e resumo
def test_outcome_backfill_covers_windows_without_position(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, min_net_edge=0.9)   # nunca aposta
    eng.step(clock.now)
    assert ledger.get(TS)["outcome"] is None
    clock.now = TS + 300 + 20
    assert eng.backfill_outcomes(clock.now) == 0            # cedo demais
    pm.completed = (STRIKE, STRIKE - 10)
    clock.now = TS + 300 + 40
    assert eng.backfill_outcomes(clock.now) == 1
    row = ledger.get(TS)
    assert row["outcome"] == "Down" and row["close_price"] == STRIKE - 10 and row["status"] != "settled"
    assert ledger.realized_pnl_usd() == 0
    assert eng.backfill_outcomes(clock.now) == 0            # não repete


def test_open_position_is_marked_to_market(tmp_path):
    clock = Clock(TS + 120)
    eng, pm, ledger, _ = build(tmp_path, clock)
    pm.after_calls = (4, TU, (0.60, 0.61))
    assert eng.step(clock.now) == "filled"
    clock.now += 30
    from tests.test_engine import make_feed
    eng.feed = make_feed(clock.now)
    assert eng.step(clock.now) == "holding"
    marks = [e for e in events(ledger) if e["event"] == "mark"]
    assert len(marks) == 1 and marks[0]["side"] == "Up" and marks[0]["bid"] == 0.60 and 0 < marks[0]["p_side"] < 1
    assert eng.step(clock.now) == "holding" and len([e for e in events(ledger) if e["event"] == "mark"]) == 1


def test_trade_kill_and_stop_alerts(tmp_path):
    clock = Clock(TS + 120)
    eng, pm, ledger, _ = build(tmp_path, clock, daily_loss_limit_usd=4.0)
    eng.notifier = spy = Spy()
    pm.after_calls = (4, TU, (0.60, 0.61))
    assert eng.step(clock.now) == "filled"
    pm.completed = (STRIKE, STRIKE - 1)
    clock.now = TS + 300 + 30
    eng.settle_pending(clock.now)
    assert spy.keys() == ["fill", "settled"] and "❌" in spy.sent[1][1]
    clock.now = TS + 300 + 60                                # próxima janela, já com o stop batido
    pm.market_obj = None
    eng.step(clock.now)
    assert spy.keys()[-1] == "daily_stop"

    eng2, _, _, _ = build(tmp_path / "k", Clock(TS + 60))
    eng2.notifier = spy2 = Spy()
    eng2.s.kill_switch.write_text("")
    eng2.step(TS + 60)
    assert spy2.keys() == ["kill_switch"]


def test_daily_summary_once_per_utc_day(tmp_path):
    day0 = TS // 86400 * 86400
    clock = Clock(day0 + 3600)
    eng, pm, ledger, _ = build(tmp_path, clock)
    eng.notifier = spy = Spy()
    eng.summary_extra = lambda day: "\nshadow alt: +1.00"
    ledger.upsert(day0 + 600, status="settled", pnl_usd=2.5, cost_usd=5.0, outcome="Up", side="Up")
    eng._daily_summary(clock.now)                            # primeiro dia: só registra
    assert spy.sent == []
    eng._daily_summary(day0 + 86400 + 60)                    # virou, mas a última janela ainda liquida
    assert spy.sent == []
    eng._daily_summary(day0 + 86400 + 200)
    eng._daily_summary(day0 + 86400 + 400)
    assert len(spy.sent) == 1
    text = spy.sent[0][1]
    assert "+2.50" in text and "1/1" in text and "shadow alt" in text


class PartialFillStuckBroker(PaperBroker):
    """Ordem com fill parcial cujo cancelamento não pega; o resto executa depois."""

    def __init__(self, book_fn):
        super().__init__(book_fn, 25.0)
        self.cancel_works = False

    def cancel(self, order_id):
        if self.cancel_works:
            return super().cancel(order_id)
        return False


def test_partial_fill_with_failed_cancel_stays_in_queue(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock)
    eng.broker = broker = PartialFillStuckBroker(pm.book)
    oid = broker.place_maker(TU, 0.61, 8.0)
    broker.orders[oid].filled, broker.orders[oid].avg_price = 3.0, 0.61
    ledger.upsert(TS, status="orphan", side="Up", limit_price=0.61, shares=8.0, order_id=oid, requotes=1)
    assert eng.resolve_open_orders(clock.now) == 0
    assert ledger.get(TS)["status"] == "orphan"              # não grava 3,0 com 5,0 ainda casáveis
    broker.orders[oid].filled = 8.0                           # o resto executou
    broker.orders[oid].open, broker.orders[oid].status = False, "MATCHED"
    clock.now += 61
    assert eng.resolve_open_orders(clock.now) == 1
    assert ledger.get(TS)["status"] == "filled" and ledger.get(TS)["filled_shares"] == pytest.approx(8.0)


def test_old_order_unknown_to_clob_is_settled_by_trades(tmp_path):
    """get_order devolve None para qualquer ordem fora do book (medido em 18/09/2026): quem decide entre
    'executou' e 'morreu' são os trades, nunca o silêncio."""
    from src.execution_5m import LiveBroker, fills_from_trades

    oid = "0x3895513543"
    trades = [  # formato real de get_trades (duas execuções da mesma ordem maker, 0,71 + 9,7 = 10,41)
        {"status": "CONFIRMED", "taker_order_id": "0xa91c", "size": "10.304073", "price": "0.53",
         "maker_orders": [{"order_id": oid, "matched_amount": "0.71", "price": "0.48"}, {"order_id": "0xc26e", "matched_amount": "5", "price": "0.47"}]},
        {"status": "CONFIRMED", "taker_order_id": "0x74dd", "size": "20.3", "price": "0.51",
         "maker_orders": [{"order_id": oid, "matched_amount": "9.7", "price": "0.48"}]},
        {"status": "FAILED", "taker_order_id": "0x0", "maker_orders": [{"order_id": oid, "matched_amount": "50", "price": "0.48"}]},
    ]
    assert fills_from_trades(trades, oid) == (pytest.approx(10.41), pytest.approx(0.48))
    assert fills_from_trades(trades, "0xoutra") == (0.0, 0.0)

    class Client:
        def __init__(self, trades):
            self.trades = trades

        def get_order(self, order_id):
            return None

        def get_trades(self, params):
            return self.trades

    import sys
    import types

    mod = types.ModuleType("py_clob_client_v2.clob_types")
    mod.TradeParams = lambda **kw: kw
    sys.modules.setdefault("py_clob_client_v2", types.ModuleType("py_clob_client_v2"))
    sys.modules.setdefault("py_clob_client_v2.clob_types", mod)

    for trades_seen, expected in ((trades, "filled"), ([], "unfilled")):
        clock = Clock(TS + 300 + 700)
        eng, pm, ledger, _ = build(tmp_path / expected, clock)
        eng.broker = LiveBroker("", "0xproxy", client=Client(trades_seen))
        ledger.upsert(TS, status="orphan", side="Up", limit_price=0.48, shares=10.41, order_id=oid, requotes=1)
        assert eng.resolve_open_orders(clock.now) == 1
        row = ledger.get(TS)
        assert row["status"] == expected
        if expected == "filled":
            assert row["filled_shares"] == pytest.approx(10.41) and row["cost_usd"] == pytest.approx(10.41 * 0.48, abs=1e-3)


def test_slow_jev_aborts_the_stale_decision_and_reuses_the_verdict(tmp_path):
    clock = Clock(TS + 60)
    eng, pm, ledger, jev = build(tmp_path, clock)
    inner = eng.jev.evaluate

    def slow(state):
        clock.now += 5                                          # fila atrás de um shadow / Jev arrastando
        return inner(state)

    eng.jev.evaluate = slow
    assert eng.step(clock.now) == "jev_slow"
    assert not [e for e in events(ledger) if e["event"] == "order_posted"]
    assert eng.step(clock.now) == "requote" and jev._cls.calls == 1   # veredito reaproveitado, dados frescos


def test_egress_stays_closed_for_a_while_after_region_403():
    clock = Clock(0)
    mon = EgressMonitor("socks5://x", interval_s=300, probe_fn=lambda s: ("1.1.1.1", "CH"), clock=clock, region_hold_s=300)
    mon.check()
    mon.mark_region_block()
    clock.now += 20
    assert not mon.check().ok and "403" in mon.reason()         # probe bom não reabre na hora
    clock.now += 300
    assert mon.check().ok


def test_order_gone_without_trade_yet_blocks_window_until_it_ends(tmp_path):
    """Cancelamento não confirmado + ordem fora do book + nenhum trade listado: pode ser fill com a API de
    trades atrasada. Não recota; só declara 'sem fill' depois do fim da janela."""
    from src.execution_5m import OrderState

    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, order_ttl_s=5)
    late_trades = {"qty": 0.0}

    class GoneBroker(PaperBroker):
        def cancel(self, order_id):
            return False

        def resolve(self, order_id, since_ts):
            q = late_trades["qty"]
            return OrderState(order_id, "", 0.61, q, filled=q, avg_price=0.61, open=False, status="GONE")

    eng.broker = broker = GoneBroker(pm.book, 25.0)
    assert eng.step(clock.now) == "cancel_unconfirmed"
    assert ledger.get(TS)["status"] == "orphan" and len(broker.orders) == 1
    clock.now += 12
    assert eng.resolve_open_orders(clock.now) == 0 and ledger.get(TS)["status"] == "orphan"
    late_trades["qty"] = 8.19                                   # o trade aparece
    assert eng.resolve_open_orders(clock.now) == 1
    assert ledger.get(TS)["status"] == "filled" and ledger.get(TS)["filled_shares"] == pytest.approx(8.19)


def test_requote_only_after_a_second_look_at_the_trades(tmp_path):
    """Fill logo antes do cancelamento, com a API de trades atrasada: o motor não pode concluir
    'morreu sem fill' na primeira olhada e postar por cima."""
    from src.execution_5m import OrderState

    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, order_ttl_s=5, max_requotes=3, requote_recheck_s=1.5)
    state = {"qty": 0.0, "calls": 0}

    class LaggingTradesBroker(PaperBroker):
        def cancel(self, order_id):
            super().cancel(order_id)
            return True                                       # o CLOB confirmou o cancelamento

        def resolve(self, order_id, since_ts):
            state["calls"] += 1
            if state["calls"] >= 2:
                state["qty"] = 8.19                           # o trade aparece na segunda consulta
            return OrderState(order_id, TU, 0.61, 8.19, filled=state["qty"], avg_price=0.61, open=False, status="GONE")

    eng.broker = LaggingTradesBroker(pm.book, 25.0)
    assert eng.step(clock.now) == "filled"
    assert state["calls"] == 2 and ledger.get(TS)["filled_shares"] == pytest.approx(8.19)


def test_open_order_after_cancel_ack_is_never_requoted(tmp_path):
    """cancel() disse OK mas o CLOB ainda mostra a ordem aberta: fechar a janela é a única saída segura."""
    from src.execution_5m import OrderState

    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, order_ttl_s=5, max_requotes=3)

    class OptimisticCancelBroker(PaperBroker):
        def cancel(self, order_id):
            return True

        def resolve(self, order_id, since_ts):
            return OrderState(order_id, TU, 0.61, 8.19, filled=0.0, open=True, status="LIVE")

    eng.broker = OptimisticCancelBroker(pm.book, 25.0)
    assert eng.step(clock.now) == "cancel_unconfirmed"
    assert ledger.get(TS)["status"] == "orphan" and eng.step(clock.now) == "done"


def test_partial_fill_with_cancel_ack_is_recorded_only_when_the_order_is_dead(tmp_path):
    from src.execution_5m import OrderState

    clock = Clock(TS + 60)
    eng, pm, ledger, _ = build(tmp_path, clock, order_ttl_s=5)

    class PartialOpenBroker(PaperBroker):
        def cancel(self, order_id):
            return True

        def resolve(self, order_id, since_ts):
            return OrderState(order_id, TU, 0.61, 8.19, filled=3.0, avg_price=0.61, open=True, status="LIVE")

    eng.broker = PartialOpenBroker(pm.book, 25.0)
    assert eng.step(clock.now) == "cancel_unconfirmed"        # 3,0 de 8,19 com o resto casável: não grava
    assert ledger.get(TS)["status"] == "orphan" and ledger.get(TS)["filled_shares"] in (0, None)


# ------------------------------------------------------------------ liquidação conferida pelo dinheiro
def test_settlement_is_corrected_by_the_money_not_by_the_price_endpoint(tmp_path):
    """Em 18/09/2026 o endpoint de preço deu o vencedor errado em 5 de 40 janelas, SEMPRE a nosso favor,
    em empates quase perfeitos (uma por US$ 0,28 em 80 mil): US$ 60 de lucro que não existia."""
    clock = Clock(TS + 120)
    eng, pm, ledger, _ = build(tmp_path, clock)
    eng.notifier = spy = Spy()
    eng.resolutions_fn = lambda since: {f"btc-updown-5m-{TS}": ("lost", 0.0)}
    pm.after_calls = (4, TU, (0.60, 0.61))
    assert eng.step(clock.now) == "filled"
    cost = ledger.get(TS)["cost_usd"]

    pm.completed = (STRIKE, STRIKE + 0.28)          # empate quase perfeito: o endpoint diz que ganhamos
    clock.now = TS + 300 + 30
    eng.settle_pending(clock.now)
    assert ledger.get(TS)["pnl_usd"] > 0            # provisório, ainda otimista

    clock.now = TS + 300 + 200                      # a carteira diz que a posição virou pó
    assert eng.reconcile_settled(clock.now) == 1
    row = ledger.get(TS)
    assert row["outcome"] == "Down" and row["pnl_usd"] == pytest.approx(-cost)
    assert row["reconciled"] == 1 and any("mismatch" in k for k in spy.keys())
    assert ledger.realized_pnl_usd() == pytest.approx(-cost)


def test_money_confirms_a_win_without_touching_the_pnl(tmp_path):
    clock = Clock(TS + 120)
    eng, pm, ledger, _ = build(tmp_path, clock)
    eng.notifier = spy = Spy()
    eng.resolutions_fn = lambda since: {f"btc-updown-5m-{TS}": ("won", 8.19)}
    pm.after_calls = (4, TU, (0.60, 0.61))
    eng.step(clock.now)
    pm.completed = (STRIKE, STRIKE + 50)
    clock.now = TS + 300 + 30
    eng.settle_pending(clock.now)
    pnl = ledger.get(TS)["pnl_usd"]
    clock.now = TS + 300 + 200
    assert eng.reconcile_settled(clock.now) == 1
    assert ledger.get(TS)["pnl_usd"] == pytest.approx(pnl) and ledger.get(TS)["reconciled"] == 1
    assert not [k for k in spy.keys() if "mismatch" in k]


def test_resolutions_separate_winner_from_loser_by_price_not_by_redeemable_flag():
    from src.wallet_watch import resolutions

    class R:
        def __init__(self, d):
            self._d = d

        def raise_for_status(self):
            pass

        def json(self):
            return self._d

    class C:
        def get(self, url, params=None):
            if "activity" in url:
                return R([] if params["offset"] else [
                    {"timestamp": 500, "type": "REDEEM", "usdcSize": 9.5, "slug": "btc-updown-5m-1"},
                    {"timestamp": 500, "type": "TRADE", "usdcSize": 5.0, "slug": "btc-updown-5m-1"},
                ])
            # "redeemable" é True nas duas: perdedora e vencedora. O preço é que separa.
            return R([{"slug": "btc-updown-5m-2", "size": 17.85, "curPrice": 0, "redeemable": True},
                      {"slug": "btc-updown-5m-3", "size": 12.0, "curPrice": 1, "redeemable": True}])

    r = resolutions("0xabc", since_ts=100, client=C())
    assert r["btc-updown-5m-1"] == ("won", 9.5)      # resgate recebido
    assert r["btc-updown-5m-2"] == ("lost", 0.0)     # sobrou valendo zero
    assert r["btc-updown-5m-3"] == ("won", 12.0)     # ganhou e ainda não resgatou

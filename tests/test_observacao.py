"""Vigia de carteira, shadows e relatório de calibração (offline)."""
import json
import threading
from pathlib import Path

import pytest

from src import calibration
from src.config import Settings, shadow_env, shadow_names
from src.ledger import Ledger
from src.notify import Notifier
from src.shadow import CachedPM, SharedJevGate
from src.wallet_watch import WalletWatch
from tests.test_engine import Clock
from tests.test_evolucao import Spy

T0 = 1789700400


def make_watch(tmp_path, clock, wallet):
    ledger = Ledger(tmp_path / "ledger.sqlite", tmp_path / "journal.jsonl")
    spy = Spy()
    w = WalletWatch(
        "0xabc", ledger, spy, tmp_path / "ref.json", divergence_usd=1.0, min_order_usd=2.5, clock=clock,
        collateral_fn=lambda: wallet["collateral"], positions_fn=lambda: wallet["positions"],
    )
    return w, ledger, spy


def settle(ledger, ts, pnl, cost=5.0):
    ledger.upsert(ts, status="settled", side="Up", outcome="Up", pnl_usd=pnl, cost_usd=cost, filled_shares=10, fill_price=cost / 10)
    ledger._conn.execute("UPDATE windows SET updated_at = 0 WHERE ts = ?", (ts,))
    ledger._conn.commit()


def test_wallet_redeem_and_consistent_pnl_do_not_alert(tmp_path):
    clock, wallet = Clock(T0), {"collateral": 20.0, "positions": []}
    w, ledger, spy = make_watch(tmp_path, clock, wallet)
    assert w.check() == "baseline"
    # Ganho de 5 ainda não resgatado. Formato REAL da data-api (medido em 18/09/2026): assim que o
    # mercado resolve, currentValue vai a zero mesmo na posição vencedora; quem vale é size, porque cada
    # share vencedora paga US$ 1.
    settle(ledger, T0, pnl=+5.0)
    wallet.update(collateral=15.0, positions=[{"slug": "btc-updown-5m-1", "size": 10.0, "curPrice": 1, "currentValue": 0.0, "redeemable": True}])
    assert w.check() == "ok"
    wallet.update(collateral=25.0, positions=[])                     # resgate: muda de bolso, não de valor
    assert w.check() == "ok"
    assert spy.sent == []


def test_wallet_divergence_needs_two_readings_then_rebases(tmp_path):
    clock, wallet = Clock(T0), {"collateral": 20.0, "positions": []}
    w, ledger, spy = make_watch(tmp_path, clock, wallet)
    w.check()
    wallet["collateral"] = 14.0                                       # US$ 6 sumiram sem o ledger saber
    assert w.check() == "pending" and spy.sent == []
    assert w.check() == "divergence"
    assert spy.keys() == ["wallet_divergence"] and "-6.00" in spy.sent[0][1]
    assert w.check() == "ok"                                          # rebaseou: não repete o mesmo alerta
    assert any(json.loads(l)["event"] == "wallet_divergence" for l in ledger.journal_path.read_text().splitlines())


def test_wallet_skips_comparison_while_position_is_open(tmp_path):
    clock, wallet = Clock(T0), {"collateral": 20.0, "positions": []}
    w, ledger, spy = make_watch(tmp_path, clock, wallet)
    w.check()
    ledger.upsert(T0, status="filled", filled_shares=10, fill_price=0.5, cost_usd=5.0)
    wallet["collateral"] = 15.0
    assert w.check() == "busy" and spy.sent == []


def test_wallet_alerts_winnings_locked_until_redeem(tmp_path):
    """O que parou o motor por 6 h em 18/09: saldo livre zerado com ganho esperando resgate."""
    clock = Clock(T0)
    wallet = {"collateral": 0.0, "positions": [{"slug": "btc-updown-5m-1", "size": 9.5, "curPrice": 1, "currentValue": 0.0, "redeemable": True},
                                               {"slug": "btc-updown-5m-2", "size": 8.0, "curPrice": 0, "currentValue": 0.0, "redeemable": True}]}
    w, ledger, spy = make_watch(tmp_path, clock, wallet)
    w.check()
    assert spy.keys() == ["wallet_locked"] and "9.50" in spy.sent[0][1]
    wallet["positions"] = []
    w2, _, spy2 = make_watch(tmp_path / "b", clock, wallet)
    w2.check()
    assert spy2.keys() == ["wallet_empty"]


def test_wallet_source_failure_is_not_a_divergence(tmp_path):
    clock, wallet = Clock(T0), {"collateral": None, "positions": []}
    w, _, spy = make_watch(tmp_path, clock, wallet)
    assert w.check() == "unavailable" and spy.sent == [] and not (tmp_path / "ref.json").exists()


# ------------------------------------------------------------------ shadows
def test_shadow_env_is_always_paper_without_wallet_key_and_in_own_dir():
    env = {"EXECUTION_MODE": "live", "POLYMARKET_PRIVATE_KEY": "segredo", "DATA_DIR": "data-live", "MIN_NET_EDGE": "0.04",
           "SHADOW_PROFILES": "control, alt", "SHADOW_ALT_FAVORED_SIDE_ONLY": "0", "SHADOW_ALT_EXECUTION_MODE": "live"}
    assert shadow_names(env) == ["control", "alt"]
    alt = Settings.from_env(shadow_env(env, "alt"))
    ctl = Settings.from_env(shadow_env(env, "control"))
    assert alt.execution_mode == "paper" and alt.polymarket_private_key is None       # nem pedindo vira live
    assert alt.favored_side_only is False and ctl.favored_side_only is True
    assert alt.min_net_edge == ctl.min_net_edge == 0.04
    # trava de dinheiro real não cala o experimento, mas pedido explícito vale
    assert alt.daily_loss_limit_usd >= 1e6 and alt.paper_bankroll_usd == 1000
    preso = Settings.from_env(shadow_env({**env, "SHADOW_ALT_DAILY_LOSS_LIMIT_USD": "7"}, "alt"))
    assert preso.daily_loss_limit_usd == 7
    assert {str(alt.data_dir), str(ctl.data_dir)} == {"data-shadow-alt", "data-shadow-control"}


def test_shared_jev_gate_reuses_verdict_inside_window_only():
    clock, calls = Clock(0), []

    class Gate:
        def evaluate(self, state):
            calls.append(state["market"]["window_start_utc"])
            return object()

    shared = SharedJevGate(Gate(), share_s=8, clock=clock)
    st = {"market": {"window_start_utc": "10:00:00"}}
    a = shared.evaluate(st)
    clock.now += 5
    assert shared.evaluate(st) is a and len(calls) == 1
    clock.now += 5
    assert shared.evaluate(st) is not a and len(calls) == 2           # venceu
    assert shared.evaluate({"market": {"window_start_utc": "10:05:00"}}) is not a and len(calls) == 3


def test_cached_pm_shares_books_and_passes_the_rest_through():
    clock = Clock(0)

    class PM:
        n = 0

        def book(self, token):
            PM.n += 1
            return f"book-{token}-{PM.n}"

        def market(self, ts):
            return f"m{ts}"

    c = CachedPM(PM(), book_ttl_s=1.5, clock=clock)
    assert c.book("a") == c.book("a") == "book-a-1" and c.book("b") == "book-b-2"
    clock.now += 2
    assert c.book("a") == "book-a-3" and c.market(7) == "m7"


# ------------------------------------------------------------------ calibração
def _journal(path: Path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_calibration_report_sections(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", tmp_path / "journal.jsonl")
    events = []
    for i in range(12):
        ts = T0 + i * 300
        up = i % 3 != 0
        ledger.upsert(ts, status="skipped", outcome="Up" if up else "Down", strike=76000.0, close_price=76000.0 * (1.001 if up else 0.999))
        p = 0.7 if up else 0.3
        base = dict(ts=ts, p_raw=p, up_bid=0.49, up_ask=0.51, cand_side="Up" if up else "Down", cand_edge_taker=0.05)
        events.append(dict(event="eval", phase=60, **base))
        events.append(dict(event="decision", phase=200, p_adj=p, side="Up" if up else "Down", limit=0.5, vetoed=None if i % 2 else "Jev dá 0.30 para Up (< 0.45)",
                           jev={"regime_probs": {"0": 0.1, "1": 0.2, "2": 0.7}, "direction_p_up": p}, **base))
    events += [dict(event="order_posted", t=10.0, order_id="a", post_ms=1800), dict(event="fill", t=14.0, order_id="a"),
               dict(event="order_posted", t=20.0, order_id="b", post_ms=900), dict(event="cancel_ttl", order_id="b"),
               dict(event="order_error", error="... order crosses book ...")]
    ledger.upsert(T0 + 9000, status="settled", side="Up", outcome="Down", pnl_usd=-5.0, cost_usd=5.0, filled_shares=10, fill_price=0.5)
    events += [dict(event="mark", ts=T0 + 9000, p_side=0.6, bid=0.55), dict(event="mark", ts=T0 + 9000, p_side=0.2, bid=0.30)]
    _journal(tmp_path / "journal.jsonl", events)

    shadow = tmp_path / "data-shadow-alt"
    sl = Ledger(shadow / "ledger.sqlite", shadow / "journal.jsonl")
    sl.upsert(T0, status="settled", pnl_usd=2.0, cost_usd=4.0, outcome="Up", side="Up")

    text = calibration.render(tmp_path, {"alt": shadow})
    assert "modelo melhor que o mercado" in text                      # p=0,7/0,3 certeiro contra mid 0,50
    assert "[0.2,0.4)" in text and "[0.6,0.8)" in text and "100%" in text and "0.090" in text
    assert " 30-89 " in text and "150-209" not in text   # só amostras regulares (eval); decision daria peso dobrado
    assert "tendência" in text and "chop" not in text.split("Por regime")[1].split("Efeito")[0]
    assert "liberadas" in text and "vetadas pelo Jev" in text
    assert "postadas 2 | fills 1 (50%)" in text and "cruzar o book 1" in text and "mediana 1350 ms" in text and "4.0 s" in text
    assert "Taker hipotético" in text and "σ realizada (n=12" in text
    # saída antecipada: 10 shares vendidas a 0,30 com taxa = 2,853 − custo 5 = −2,15 contra −5,00 segurando
    assert "gatilho em 1" in text and "segurando -5.00" in text and "saindo -2.15" in text
    assert "alt" in text and "+2.00" in text and "Abaixo de ~100" in text


def test_calibration_reads_ledger_without_writing(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", tmp_path / "journal.jsonl")
    ledger.upsert(T0, status="seen")
    before = (tmp_path / "ledger.sqlite").stat().st_mtime_ns
    calibration.render(tmp_path)
    assert (tmp_path / "ledger.sqlite").stat().st_mtime_ns == before
    assert "Janelas com resultado conhecido: 0 de 1" in calibration.render(tmp_path)


def test_notifier_mono_block_is_escaped_and_bounded():
    sent = []
    n = Notifier("T", "C", post=lambda url, payload: sent.append(payload), start=False)
    n.send("k", "título <b>", 0, mono="p < 0.35 & x\n" * 600)
    n.drain_once()
    body = sent[0]["text"]
    assert sent[0]["parse_mode"] == "HTML" and body.startswith("título &lt;b&gt;\n<pre>p &lt; 0.35 &amp; x")
    assert body.endswith("</pre>") and len(body) < 4096


def test_shared_jev_gate_shares_a_failure_instead_of_queueing_timeouts():
    clock, calls = Clock(0), []

    class Down:
        def evaluate(self, state):
            calls.append(1)
            raise TimeoutError("jev fora")

    shared = SharedJevGate(Down(), share_s=8, clock=clock)
    st = {"market": {"window_start_utc": "10:00:00"}}
    with pytest.raises(TimeoutError):
        shared.evaluate(st)                                   # quem provocou a falha vê a original
    errs = []
    for _ in range(3):
        with pytest.raises(Exception) as ei:                  # instância nova a cada vez: traceback não acumula
            shared.evaluate(st)
        errs.append(ei.value)
    assert len(calls) == 1 and all("jev fora" in str(e) for e in errs)
    assert len({id(e) for e in errs}) == 3
    clock.now += 9
    with pytest.raises(TimeoutError):
        shared.evaluate(st)
    assert len(calls) == 2


# ------------------------------------------------------------------ comparação pareada
def test_paired_comparison_only_uses_shared_windows():
    from src.calibration import paired

    a = {100: +5.0, 200: -1.0, 300: +9.9}      # 300 só existe em "a": entra no total bruto, não na comparação
    b = {100: +1.0, 200: -1.0}
    r = paired(a, b)
    assert r["n"] == 2 and r["total"] == pytest.approx(4.0) and r["mean"] == pytest.approx(2.0)
    assert r["wins"] == 1 and r["ties"] == 1
    assert paired({}, b)["n"] == 0


def test_paired_comparison_says_when_it_is_still_noise():
    from src.calibration import paired

    vals = [5.0 if i % 2 else -5.0 for i in range(20)]
    vals[0] = -4.0                                               # média minúscula, variância enorme
    ruido = paired(dict(enumerate(vals)), {i: 0.0 for i in range(20)})
    assert not ruido["conclusive"] and ruido["need"] > 1000      # nessa toada não se decide nunca

    empate = paired({i: 1.0 for i in range(6)}, {i: 1.0 for i in range(6)})
    assert not empate["conclusive"] and empate["need"] is None    # diferença exatamente zero
    claro = paired({i: 2.0 for i in range(20)}, {i: 0.0 for i in range(20)})
    assert claro["conclusive"] and claro["mean"] == pytest.approx(2.0)


def test_report_compares_against_control_not_against_the_live(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", tmp_path / "journal.jsonl")
    ledger.upsert(T0, status="settled", pnl_usd=+9.0, cost_usd=5.0, outcome="Up", side="Up")   # live "sortudo"
    dirs = {}
    for name, pnl in (("control", -1.0), ("alt", +3.0)):
        d = tmp_path / f"data-shadow-{name}"
        sl = Ledger(d / "ledger.sqlite", d / "journal.jsonl")
        for i, v in enumerate((pnl, pnl)):
            sl.upsert(T0 + 300 * i, status="settled", pnl_usd=v, cost_usd=5.0, outcome="Up", side="Up")
        dirs[name] = d
    text = calibration.render(tmp_path, dirs)
    assert "referência: shadow control" in text
    assert "alt" in text and "+8.00" in text                     # 2 janelas x (+3 - (-1))
    assert "não comparáveis entre si" in text


def test_fill_rate_section_and_ledger_measure(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", tmp_path / "journal.jsonl")
    assert ledger.maker_fill_rate() is None                      # amostra curta não vira taxa
    for i in range(12):
        ledger.upsert(T0 + 300 * i, status="filled" if i % 4 else "unfilled", entry_kind="maker")
    assert ledger.maker_fill_rate() == pytest.approx(9 / 12)
    ledger.upsert(T0 + 99999, status="filled", entry_kind="taker")
    assert ledger.maker_fill_rate() == pytest.approx(9 / 12)     # taker não entra na conta do maker
    ledger.upsert(T0 - 300, status="skipped", entry_kind="maker", reason="recotações esgotadas")
    assert ledger.maker_fill_rate() == pytest.approx(9 / 13)     # postou e não entrou também conta
    ledger.upsert(T0 - 600, status="skipped", reason="sem edge")
    assert ledger.maker_fill_rate() == pytest.approx(9 / 13)     # janela que nem postou fica de fora
    assert "maker: 9/13 (69%)" in calibration.render(tmp_path)


def test_wallet_values_a_won_position_by_size_not_by_current_value(tmp_path):
    """A posição vencedora não resgatada aparece com currentValue 0 na data-api. Lendo currentValue, a
    carteira parecia US$ 60 mais pobre que o ledger e o vigia disparava divergência falsa."""
    clock = Clock(T0)
    wallet = {"collateral": 1.0, "positions": [                                                # saldo livre não paga ordem
        {"slug": "btc-updown-5m-1", "size": 17.85, "curPrice": 1, "currentValue": 0.0, "redeemable": True},  # ganhou
        {"slug": "btc-updown-5m-2", "size": 9.0, "curPrice": 0, "currentValue": 0.0, "redeemable": True},    # perdeu (redeemable também!)
    ]}
    w, ledger, spy = make_watch(tmp_path, clock, wallet)
    rd = w.read()
    assert rd.redeemable_value == pytest.approx(17.85) and rd.positions_value == pytest.approx(17.85)
    assert rd.actual == pytest.approx(18.85)
    w.check()
    assert spy.keys() == ["wallet_locked"] and "17.85" in spy.sent[0][1]


def test_wallet_flow_reads_real_money_in_and_out():
    from src.wallet_watch import wallet_flow

    class FakeResp:
        def __init__(self, data):
            self._d = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._d

    class FakeClient:
        def get(self, url, params=None):
            if "activity" in url:
                if params["offset"]:
                    return FakeResp([])
                return FakeResp([
                    {"timestamp": 100, "type": "TRADE", "side": "BUY", "usdcSize": 5.0, "slug": "btc-updown-5m-1"},
                    {"timestamp": 120, "type": "REDEEM", "usdcSize": 9.5, "slug": "btc-updown-5m-1"},
                    {"timestamp": 130, "type": "TRADE", "side": "BUY", "usdcSize": 4.0, "slug": "outro-mercado"},
                    {"timestamp": 50, "type": "TRADE", "side": "BUY", "usdcSize": 99.0, "slug": "btc-updown-5m-0"},
                ])
            return FakeResp([{"slug": "btc-updown-5m-2", "size": 12.0, "curPrice": 1, "currentValue": 0.0, "redeemable": True},
                             {"slug": "btc-updown-5m-3", "size": 8.0, "curPrice": 0, "currentValue": 0.0, "redeemable": True}])

    f = wallet_flow("0xabc", since_ts=90, client=FakeClient())
    assert f["comprado"] == 5.0 and f["resgatado"] == 9.5      # fora da janela de tempo e de outro mercado: ignorados
    assert f["pendente"] == 12.0 and f["pendentes"] == 1.0

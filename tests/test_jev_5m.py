import json
from types import SimpleNamespace

from src.jev_5m import JevGate, JevVerdict, build_state, parse_response, veto


def _state(**over):
    base = dict(
        ts_start=1789697400, ts_end=1789697700, now=1789697520.0, price_to_beat=76643.29,
        chainlink_now=76690.10, sample_age_s=0.8, samples_last_60s=58, crossings=1,
        window_low=76630.0, window_high=76695.0,
        returns_pct={"30s": 0.02, "60s": 0.05, "180s": 0.06, "300s": None, "900s": -0.1},
        sigma_5m_usd=41.2, sigma_ratio_5m_vs_15m=1.1, typical_abs_move_5m_usd=38.5,
    )
    base.update(over)
    return build_state(**base)


def test_state_has_no_market_odds():
    s = _state()
    text = json.dumps(s).lower()
    for banned in ("odds", "edge", "ask", "bid", "yes_price", "implied", "kelly", "0.49", "0.51"):
        assert banned not in text, banned
    assert s["distance_from_price_to_beat"]["usd"] == 46.81
    assert s["market"]["seconds_remaining"] == 180
    assert "tie resolves up" in s["market"]["resolution_rule"].lower()
    assert s["path_this_window"]["current_position_in_window_range_pct"] == 92.5


def _fake_resp(score=1.4, probs=None, conf=0.6, anomaly=0.1, direction=0.7):
    probs = probs if probs is not None else {0: 0.1, 1: 0.4, 2: 0.5}
    return SimpleNamespace(
        scores={"regime": SimpleNamespace(score=score, probabilities=probs, confidence=conf)},
        nouls={"anomaly": SimpleNamespace(noul=anomaly), "direction_up": SimpleNamespace(noul=direction)},
    )


def test_parse_response_reads_probabilities_by_level():
    v = parse_response(_fake_resp(probs={"0": 0.2, "1": 0.3, "2": 0.5}), latency_ms=123)
    assert v.p_chop == 0.2 and v.p_trend == 0.5 and v.regime_score == 1.4
    assert v.anomaly_p == 0.1 and v.direction_p_up == 0.7 and v.latency_ms == 123


def test_parse_response_remaps_one_based_levels():
    v = parse_response(_fake_resp(probs={1: 0.2, 2: 0.3, 3: 0.5}))
    assert v.p_chop == 0.2 and v.p_trend == 0.5


def test_gate_uses_injected_client():
    calls = []

    class FakeClient:
        def system_one(self, state, questions):
            calls.append((state, sorted(questions)))
            return _fake_resp()

    g = JevGate(client_factory=lambda: FakeClient())
    v = g.evaluate({"x": 1})
    assert isinstance(v, JevVerdict)
    assert calls[0][1] == ["anomaly", "direction_up", "regime"]


def test_veto_policy():
    ok = parse_response(_fake_resp(anomaly=0.2, direction=0.7))
    assert veto(ok, "Up", 0.5, 0.45) is None
    assert veto(ok, "Down", 0.5, 0.45) is not None          # Jev dá 0,30 para Down
    anomalous = parse_response(_fake_resp(anomaly=0.8, direction=0.7))
    assert "anomalia" in veto(anomalous, "Up", 0.5, 0.45)
    borderline = parse_response(_fake_resp(anomaly=0.2, direction=0.5))
    assert veto(borderline, "Up", 0.5, 0.45) is None

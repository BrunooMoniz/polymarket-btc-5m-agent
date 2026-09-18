import json
import math

from src.chainlink_feed import PriceBuffer, parse_frame


def test_parse_frame_handles_empty_and_garbage():
    assert parse_frame("") == []
    assert parse_frame("   ") == []
    assert parse_frame("not json") == []
    assert parse_frame(json.dumps({"topic": "other", "payload": {"data": [{"timestamp": 1, "value": 2}]}})) == []


def test_parse_frame_subscribe_backfill(fixture_json):
    s = parse_frame(json.dumps(fixture_json("ws_frame.json")))
    assert len(s) == 5
    assert s[0] == (1789697325.0, 76607.90942633283)


def test_parse_frame_single_update(fixture_json):
    s = parse_frame(json.dumps(fixture_json("ws_update_frame.json")))
    assert s == [(1789698805.0, 76904.86308162566)]


def test_parse_frame_rejects_other_symbol(fixture_json):
    obj = fixture_json("ws_update_frame.json")
    obj["payload"]["symbol"] = "eth/usd"
    assert parse_frame(json.dumps(obj)) == []


def test_buffer_orders_dedupes_and_ages():
    b = PriceBuffer()
    b.add_many([(10.0, 100.0), (12.0, 101.0), (11.0, 100.5), (12.0, 101.5)])
    assert len(b) == 3  # add_many ordena; ts repetido mantém o último valor
    assert b.latest() == (12.0, 101.5)
    assert b.age_s(15.0) == 3.0
    assert b.value_at(11.5) == 100.5
    assert b.value_at(9.0) is None
    b.add(11.0, 999.0)  # atrasado: ignorado
    assert len(b) == 3 and b.value_at(11.5) == 100.5


def test_sigma_and_returns_and_crossings():
    b = PriceBuffer()
    px = 100.0
    vals = []
    for i in range(200):
        px *= math.exp(0.001 if i % 2 == 0 else -0.001)
        vals.append((float(i), px))
    b.add_many(vals)
    sig = b.sigma_1s(199.0, 300)
    assert sig is not None and 0.0009 < sig < 0.0011
    r = b.log_return(199.0, 100)
    assert r is not None
    # série alterna em torno de ~100: muitos cruzamentos do nível 100.05
    assert b.crossings(0.0, 100.05) > 50
    lo, hi = b.extremes(0.0)
    assert lo < 100.05 < hi


def test_sigma_with_30s_step_on_random_walk():
    import random

    rnd = random.Random(7)
    b = PriceBuffer()
    px = 76000.0
    vals = []
    for i in range(3600):
        px *= math.exp(rnd.gauss(0.0, 5e-5))
        vals.append((float(i), px))
    b.add_many(vals)
    s1 = b.sigma_1s(3599.0, 1800, 1.0)
    s30 = b.sigma_1s(3599.0, 1800, 30.0)
    assert s1 is not None and s30 is not None
    # passeio aleatório puro: as duas estimativas concordam (dentro de 25%)
    assert abs(s30 / s1 - 1.0) < 0.25
    assert 3.5e-5 < s30 < 6.5e-5


def test_sigma_30s_step_sees_through_smoothing():
    b = PriceBuffer()
    # série suavizada: sobe 0,5 USD/s por 60 s, depois desce, sem ruído por segundo
    px = 76000.0
    vals = []
    for i in range(3600):
        px += 0.5 if (i // 60) % 2 == 0 else -0.5
        vals.append((float(i), px))
    b.add_many(vals)
    s1 = b.sigma_1s(3599.0, 1800, 1.0)
    s30 = b.sigma_1s(3599.0, 1800, 30.0)
    assert s1 is not None and s30 is not None and s30 > s1 * 3


def test_sigma_needs_enough_samples():
    b = PriceBuffer()
    b.add_many([(float(i), 100.0 + i) for i in range(10)])
    assert b.sigma_1s(9.0, 300) is None

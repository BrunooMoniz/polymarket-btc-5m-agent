import pytest

from src.model import best_candidate, maker_limit, norm_cdf, p_up, regime_multiplier, shares_for
from src.polymarket_5m import Book


def book(bid, ask, ts=0.0):
    return Book(best_bid=bid, best_ask=ask, bid_size=100, ask_size=100, ts=ts)


def test_norm_cdf():
    assert norm_cdf(0) == pytest.approx(0.5)
    assert norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)


def test_p_up_shape():
    assert p_up(0.0, 76000, 5e-5, 120) == pytest.approx(0.5)
    assert p_up(200.0, 76000, 5e-5, 120) > 0.99
    assert p_up(-200.0, 76000, 5e-5, 120) < 0.01
    # mais tempo restante => mais incerteza
    assert p_up(30.0, 76000, 5e-5, 240) < p_up(30.0, 76000, 5e-5, 30)
    # tau zero vira degrau; empate conta como Up
    assert p_up(0.0, 76000, 5e-5, 0) == 1.0
    assert p_up(-0.01, 76000, 5e-5, 0) == 0.0
    # chop (multiplicador > 1) puxa para 0,5
    assert abs(p_up(30.0, 76000, 5e-5, 120, 1.6) - 0.5) < abs(p_up(30.0, 76000, 5e-5, 120, 1.0) - 0.5)


def test_regime_multiplier_bounds():
    assert regime_multiplier(1.0, 0.0) == pytest.approx(1.6)
    assert regime_multiplier(0.0, 1.0) == pytest.approx(0.8)
    assert regime_multiplier(0.34, 0.33) == pytest.approx(1.0 + 0.6 * 0.34 - 0.25 * 0.33)


def test_maker_limit_never_crosses():
    assert maker_limit(book(0.47, 0.50), 0.01) == pytest.approx(0.48)
    assert maker_limit(book(0.49, 0.50), 0.01) == pytest.approx(0.49)
    assert maker_limit(book(None, 0.01), 0.01) is None
    assert maker_limit(book(0.49, None), 0.01) is None


def test_best_candidate_prefers_side_with_edge():
    up = book(0.30, 0.32)
    down = book(0.66, 0.70)
    c = best_candidate(0.80, up, down, "TU", "TD", 0.01, 0.07)
    assert c.side == "Up" and c.token_id == "TU" and c.limit_price == pytest.approx(0.31)
    assert c.edge_maker == pytest.approx(0.80 - 0.31)
    assert c.edge_taker == pytest.approx(0.80 - 0.32 - 0.07 * 0.32 * 0.68)
    d = best_candidate(0.20, up, down, "TU", "TD", 0.01, 0.07)
    assert d.side == "Down" and d.limit_price == pytest.approx(0.67)
    assert best_candidate(0.5, None, None, "TU", "TD", 0.01, 0.07) is None


def test_shares_for_respects_floors():
    assert shares_for(5.0, 0.48, 5, 1.0) == pytest.approx(10.41)
    assert shares_for(2.6, 0.5, 5, 1.0) == pytest.approx(5.2)
    assert shares_for(2.0, 0.5, 5, 1.0) is None       # 5 shares custam 2,50 > 2,00
    assert shares_for(1.53, 0.30, 5, 1.0) == pytest.approx(5.1)
    assert shares_for(0.0, 0.5, 5, 1.0) is None

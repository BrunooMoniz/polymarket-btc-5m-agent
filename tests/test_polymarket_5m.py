import pytest

from src.polymarket_5m import (
    WINDOW_S,
    iso_utc,
    outcome_from_prices,
    parse_book,
    parse_crypto_price,
    parse_gamma_market,
    slug_for,
    taker_fee_per_share,
    window_start,
)


def test_window_math():
    assert window_start(1789697404.7) == 1789697400
    assert window_start(1789697399.9) == 1789697100
    assert slug_for(1789697400) == "btc-updown-5m-1789697400"
    assert iso_utc(1789697400) == "2026-09-18T02:10:00Z"
    assert WINDOW_S == 300


def test_parse_gamma_market_maps_up_to_token0(fixture_json):
    m = parse_gamma_market(fixture_json("gamma_market.json")[0], 1789697400)
    assert m.token_up.startswith("314793364188")
    assert m.token_down.startswith("143226321134")
    assert m.tick == 0.01 and m.min_size == 5 and m.fee_rate == 0.07
    assert m.accepting_orders and not m.closed
    assert m.ts_end == 1789697700


def test_parse_gamma_market_rejects_other_outcomes(fixture_json):
    obj = dict(fixture_json("gamma_market.json")[0])
    obj["outcomes"] = '["Yes", "No"]'
    with pytest.raises(ValueError):
        parse_gamma_market(obj, 1789697400)


def test_parse_book_picks_best_levels(fixture_json):
    b = parse_book(fixture_json("clob_book.json"), ts=1.0)
    assert b.best_bid == 0.49 and b.bid_size == 40
    assert b.best_ask == 0.5 and b.ask_size == 107.4


def test_parse_book_empty_side():
    b = parse_book({"bids": [], "asks": [{"price": "0.01", "size": "8065"}]}, ts=1.0)
    assert b.best_bid is None and b.best_ask == 0.01


def test_parse_crypto_price_incomplete_and_complete():
    p = parse_crypto_price({"openPrice": 76643.29, "closePrice": None, "completed": False})
    assert p.open_price == 76643.29 and p.close_price is None and not p.completed
    q = parse_crypto_price({"openPrice": 76560.85, "closePrice": 76628.83, "completed": True})
    assert q.completed and q.close_price == 76628.83


def test_outcome_tie_is_up():
    assert outcome_from_prices(100.0, 100.0) == "Up"
    assert outcome_from_prices(100.0, 100.01) == "Up"
    assert outcome_from_prices(100.0, 99.99) == "Down"


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _Client:
    def __init__(self, data):
        self.data = data

    def get(self, url, params=None):
        return _Resp(self.data)


def test_gamma_outcome_requires_closed_and_up_down_order(fixture_json):
    from src.polymarket_5m import PolymarketPublic

    m = dict(fixture_json("gamma_market.json")[0])
    assert PolymarketPublic(client=_Client([m])).gamma_outcome(1789697400) is None  # aberto
    m["closed"] = True
    m["outcomePrices"] = '["1", "0"]'
    assert PolymarketPublic(client=_Client([m])).gamma_outcome(1789697400) == "Up"
    m["outcomePrices"] = '["0", "1"]'
    assert PolymarketPublic(client=_Client([m])).gamma_outcome(1789697400) == "Down"
    m["outcomes"] = '["Down", "Up"]'
    assert PolymarketPublic(client=_Client([m])).gamma_outcome(1789697400) is None  # ordem inesperada
    assert PolymarketPublic(client=_Client([])).gamma_outcome(1789697400) is None


def test_taker_fee_formula():
    assert taker_fee_per_share(0.5) == pytest.approx(0.0175)
    assert taker_fee_per_share(0.7) == pytest.approx(0.0147)

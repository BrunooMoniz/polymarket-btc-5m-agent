"""Um motor por ativo: slug, símbolo, feed, prior e rótulo do Jev acompanham o ativo escolhido."""
import json

import pytest

from src.assets import spec_for
from src.chainlink_feed import parse_frame, subscribe_msg
from src.config import Settings, shadow_env
from src.polymarket_5m import PolymarketPublic, slug_for


def test_asset_specs_are_distinct_and_measured():
    btc, eth, sol = spec_for("btc"), spec_for("ETH"), spec_for("sol")
    assert (btc.symbol, btc.feed_symbol, btc.slug_prefix) == ("BTC", "btc/usd", "btc-updown-5m-")
    assert (eth.symbol, eth.feed_symbol, eth.slug_prefix) == ("ETH", "eth/usd", "eth-updown-5m-")
    # σ medida em 79 janelas (18/09/2026): SOL oscila mais que ETH, que oscila mais que BTC
    assert sol.sigma_prior_1s > eth.sigma_prior_1s > btc.sigma_prior_1s
    assert btc.typical_abs_move_5m > eth.typical_abs_move_5m > sol.typical_abs_move_5m
    with pytest.raises(ValueError, match="ativo desconhecido"):
        spec_for("pepe")


def test_settings_take_the_asset_defaults_and_allow_override():
    s = Settings.from_env({"ASSET": "eth"})
    assert s.asset == "eth" and s.sigma_prior_1s == spec_for("eth").sigma_prior_1s
    assert s.typical_abs_move_5m == spec_for("eth").typical_abs_move_5m and s.spec.label == "ETH/USD"
    s2 = Settings.from_env({"ASSET": "sol", "SIGMA_PRIOR_1S": "9e-5"})
    assert s2.sigma_prior_1s == 9e-5                      # medida própria vence o default do ativo
    assert Settings.from_env({}).asset == "btc"           # sem ASSET, nada muda para quem já rodava


def test_shadow_of_another_asset_does_not_inherit_the_btc_prior():
    env = {"ASSET": "btc", "SIGMA_PRIOR_1S": "5.7e-5", "TYPICAL_ABS_MOVE_5M": "36.6",
           "SHADOW_PROFILES": "eth", "SHADOW_ETH_ASSET": "eth"}
    eth = Settings.from_env(shadow_env(env, "eth"))
    assert eth.asset == "eth" and eth.sigma_prior_1s == spec_for("eth").sigma_prior_1s
    assert eth.typical_abs_move_5m == spec_for("eth").typical_abs_move_5m
    assert str(eth.data_dir) == "data-shadow-eth"
    # shadow do mesmo ativo continua herdando o que o live usa
    mesmo = Settings.from_env(shadow_env({**env, "SHADOW_PROFILES": "control"}, "control"))
    assert mesmo.asset == "btc" and mesmo.sigma_prior_1s == 5.7e-5


def test_market_and_feed_follow_the_asset():
    assert slug_for(1789697400, "eth-updown-5m-") == "eth-updown-5m-1789697400"
    pm = PolymarketPublic(asset="sol")
    assert pm.slug(1789697400) == "sol-updown-5m-1789697400" and pm.spec.symbol == "SOL"

    filtros = json.loads(subscribe_msg("eth/usd")["subscriptions"][0]["filters"])
    assert filtros["symbol"] == "eth/usd"

    frame = json.dumps({"topic": "crypto_prices_chainlink",
                        "payload": {"timestamp": 1789697400000, "value": 2635.61, "symbol": "eth/usd"}})
    assert parse_frame(frame, "eth/usd") == [(1789697400.0, 2635.61)]
    assert parse_frame(frame, "btc/usd") == []            # frame de outro ativo não entra no buffer errado


def test_jev_questions_name_the_right_asset():
    from src.jev_5m import build_state, questions

    class FakeNoul:
        def __init__(self, instructions=None, **kw):
            self.instructions = instructions

    import sys
    import types

    mod = types.ModuleType("typesafe_sdk")
    mod.Noul = FakeNoul
    mod.Score = lambda instructions=None, criteria=None: FakeNoul(instructions)
    sys.modules["typesafe_sdk"] = mod

    q = questions("direction", "SOL/USD")
    assert "SOL/USD" in q["direction_up"].instructions and "BTC" not in q["direction_up"].instructions
    st = build_state(ts_start=0, ts_end=300, now=10, price_to_beat=113.8, chainlink_now=113.9,
                     sample_age_s=0.5, samples_last_60s=60, crossings=0, window_low=113.0, window_high=114.0,
                     returns_pct={"30s": 0.1}, sigma_5m_usd=0.3, sigma_ratio_5m_vs_15m=1.0,
                     typical_abs_move_5m_usd=0.17, asset_label="SOL/USD")
    assert st["market"]["asset"] == "SOL/USD"

"""Acesso público à Polymarket para o mercado btc-updown-5m: Gamma (mercado por slug),
crypto-price (Price to Beat e fechamento Chainlink) e CLOB (book). Sem chave, sem escrita."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

log = logging.getLogger("polymarket_5m")

WINDOW_S = 300
GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CRYPTO_PRICE_URL = "https://polymarket.com/api/crypto/crypto-price"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
# Gamma e polymarket.com devolvem 403 para user-agents de biblioteca; header de navegador é obrigatório.
HEADERS = {
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) jev-5m-agent/1.0",
    "accept": "application/json",
}


def window_start(now: float) -> int:
    return int(now) // WINDOW_S * WINDOW_S


def slug_for(ts_start: int) -> str:
    return f"btc-updown-5m-{ts_start}"


def iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def taker_fee_per_share(price: float, rate: float = 0.07) -> float:
    """crypto_fees_v2, expoente 1: taxa taker por share = rate * p * (1-p). Maker paga zero."""
    return rate * price * (1.0 - price)


@dataclass(frozen=True)
class Market5m:
    slug: str
    ts_start: int
    ts_end: int
    market_id: str
    condition_id: str
    token_up: str
    token_down: str
    tick: float
    min_size: float
    fee_rate: float
    accepting_orders: bool
    closed: bool


@dataclass(frozen=True)
class Book:
    best_bid: Optional[float]
    best_ask: Optional[float]
    bid_size: float
    ask_size: float
    ts: float


@dataclass(frozen=True)
class PriceToBeat:
    open_price: Optional[float]
    close_price: Optional[float]
    completed: bool


def _loads_maybe(v: Any) -> Any:
    if isinstance(v, str):
        return json.loads(v)
    return v


def parse_gamma_market(obj: dict, ts_start: int) -> Market5m:
    outcomes = _loads_maybe(obj.get("outcomes"))
    tokens = _loads_maybe(obj.get("clobTokenIds"))
    if outcomes != ["Up", "Down"]:
        raise ValueError(f"outcomes inesperados: {outcomes!r}")
    if not isinstance(tokens, list) or len(tokens) != 2:
        raise ValueError(f"clobTokenIds inesperados: {tokens!r}")
    fee = obj.get("feeSchedule") or {}
    fee_rate = float(fee.get("rate", 0.07)) if isinstance(fee, dict) else 0.07
    return Market5m(
        slug=str(obj.get("slug") or slug_for(ts_start)),
        ts_start=ts_start,
        ts_end=ts_start + WINDOW_S,
        market_id=str(obj.get("id")),
        condition_id=str(obj.get("conditionId")),
        token_up=str(tokens[0]),
        token_down=str(tokens[1]),
        tick=float(obj.get("orderPriceMinTickSize") or 0.01),
        min_size=float(obj.get("orderMinSize") or 5),
        fee_rate=fee_rate,
        accepting_orders=bool(obj.get("acceptingOrders", False)),
        closed=bool(obj.get("closed", False)),
    )


def parse_book(obj: dict, ts: Optional[float] = None) -> Book:
    bids = [(float(b["price"]), float(b["size"])) for b in obj.get("bids", []) if float(b.get("size", 0)) > 0]
    asks = [(float(a["price"]), float(a["size"])) for a in obj.get("asks", []) if float(a.get("size", 0)) > 0]
    best_bid = max(bids, key=lambda x: x[0]) if bids else None
    best_ask = min(asks, key=lambda x: x[0]) if asks else None
    return Book(
        best_bid=best_bid[0] if best_bid else None,
        best_ask=best_ask[0] if best_ask else None,
        bid_size=best_bid[1] if best_bid else 0.0,
        ask_size=best_ask[1] if best_ask else 0.0,
        ts=time.time() if ts is None else ts,
    )


def parse_crypto_price(obj: dict) -> PriceToBeat:
    op = obj.get("openPrice")
    cp = obj.get("closePrice")
    return PriceToBeat(
        open_price=float(op) if op is not None else None,
        close_price=float(cp) if cp is not None else None,
        completed=bool(obj.get("completed", False)),
    )


def outcome_from_prices(open_price: float, close_price: float) -> str:
    """Regra oficial: fechamento >= abertura resolve Up (empate = Up)."""
    return "Up" if close_price >= open_price else "Down"


class PolymarketPublic:
    def __init__(self, client: Optional[httpx.Client] = None, timeout: float = 6.0):
        self._client = client or httpx.Client(headers=HEADERS, timeout=timeout)

    def _get(self, url: str, params: dict) -> Any:
        r = self._client.get(url, params=params)
        r.raise_for_status()
        return r.json()

    def market(self, ts_start: int) -> Optional[Market5m]:
        try:
            data = self._get(GAMMA_URL, {"slug": slug_for(ts_start)})
        except Exception as e:  # rede/403: falha fechada, quem chama pula a janela
            log.warning("gamma falhou para %s: %s", slug_for(ts_start), e)
            return None
        if not data:
            return None
        try:
            return parse_gamma_market(data[0], ts_start)
        except Exception as e:
            log.warning("mercado %s com formato inesperado: %s", slug_for(ts_start), e)
            return None

    def price_to_beat(self, ts_start: int) -> Optional[PriceToBeat]:
        params = {
            "symbol": "BTC",
            "variant": "fiveminute",
            "eventStartTime": iso_utc(ts_start),
            "endTime": iso_utc(ts_start + WINDOW_S),
        }
        try:
            return parse_crypto_price(self._get(CRYPTO_PRICE_URL, params))
        except Exception as e:
            log.warning("crypto-price falhou para %s: %s", ts_start, e)
            return None

    def book(self, token_id: str) -> Optional[Book]:
        try:
            return parse_book(self._get(CLOB_BOOK_URL, {"token_id": token_id}))
        except Exception as e:
            log.warning("book falhou para %s...: %s", token_id[:12], e)
            return None

    def gamma_outcome(self, ts_start: int) -> Optional[str]:
        """Fallback de liquidação: outcomePrices ["1","0"] = Up, ["0","1"] = Down (só quando fechado)."""
        try:
            data = self._get(GAMMA_URL, {"slug": slug_for(ts_start)})
        except Exception:
            return None
        if not data:
            return None
        m = data[0]
        if not m.get("closed"):
            return None
        try:
            if _loads_maybe(m.get("outcomes")) != ["Up", "Down"]:
                return None
            prices = _loads_maybe(m.get("outcomePrices")) or []
        except Exception:
            return None
        try:
            up, down = float(prices[0]), float(prices[1])
        except Exception:
            return None
        if up == 1.0 and down == 0.0:
            return "Up"
        if up == 0.0 and down == 1.0:
            return "Down"
        return None

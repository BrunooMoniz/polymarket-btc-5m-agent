"""Feed Chainlink BTC/USD ao vivo (a mesma série que resolve o mercado 5m), via
wss://ws-live-data.polymarket.com. Buffer thread-safe com vol realizada, cruzamentos e staleness.
A vol inicial pode ser semeada da Binance (só volatilidade, nunca nível de preço)."""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from bisect import bisect_left, bisect_right
from collections import deque
from typing import Deque, List, Optional, Tuple

log = logging.getLogger("chainlink_feed")

WS_URL = "wss://ws-live-data.polymarket.com"
# Único envelope que produz dados (verificado 18/09/2026); o formato "topic" solto não devolve nada.
def subscribe_msg(symbol: str = "btc/usd") -> dict:
    return {"action": "subscribe", "subscriptions": [
        {"topic": "crypto_prices_chainlink", "type": "*", "filters": json.dumps({"symbol": symbol})}]}


SUBSCRIBE = subscribe_msg()
WS_HEADERS = {
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) jev-5m-agent/1.0",
    "origin": "https://polymarket.com",
}

Sample = Tuple[float, float]  # (ts_seconds, value_usd)


ACCEPTED_TOPICS = (None, "crypto_prices", "crypto_prices_chainlink")


def parse_frame(text: str, symbol: str = "btc/usd") -> List[Sample]:
    """Extrai amostras de um frame do WS. Formatos observados (18/09/2026):
    - primeiro frame vazio;
    - resposta ao subscribe: topic "crypto_prices", type "subscribe", payload.data = lista (~60 s de histórico);
    - atualização: topic "crypto_prices_chainlink", type "update", payload = {timestamp, value, symbol}.
    Frames de outros tópicos/símbolos ou malformados devolvem lista vazia em vez de levantar."""
    if not text or not text.strip():
        return []
    try:
        obj = json.loads(text)
    except Exception:
        return []
    if not isinstance(obj, dict):
        return []
    if obj.get("topic") not in ACCEPTED_TOPICS:
        return []
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return []
    got = payload.get("symbol")
    if got is not None and str(got).lower() != symbol:
        return []
    data = payload.get("data")
    items = data if isinstance(data, list) else [payload]
    out: List[Sample] = []
    for d in items:
        if not isinstance(d, dict):
            continue
        try:
            ts_ms = float(d["timestamp"])
            val = float(d["value"])
        except Exception:
            continue
        if val <= 0 or ts_ms <= 0:
            continue
        out.append((ts_ms / 1000.0, val))
    return out


class PriceBuffer:
    """Série ordenada por timestamp da fonte (não do recebimento)."""

    def __init__(self, maxlen: int = 7200):
        self._ts: Deque[float] = deque(maxlen=maxlen)
        self._val: Deque[float] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, ts: float, value: float) -> None:
        with self._lock:
            if self._ts and ts <= self._ts[-1]:
                if ts == self._ts[-1]:
                    self._val[-1] = value
                return  # atrasado/duplicado: ignora
            self._ts.append(ts)
            self._val.append(value)

    def add_many(self, samples: List[Sample]) -> None:
        for ts, v in sorted(samples):
            self.add(ts, v)

    def __len__(self) -> int:
        return len(self._ts)

    def latest(self) -> Optional[Sample]:
        with self._lock:
            if not self._ts:
                return None
            return (self._ts[-1], self._val[-1])

    def age_s(self, now: float) -> Optional[float]:
        lt = self.latest()
        return None if lt is None else now - lt[0]

    def _snapshot(self) -> Tuple[List[float], List[float]]:
        with self._lock:
            return list(self._ts), list(self._val)

    def value_at(self, ts: float) -> Optional[float]:
        """Último valor com timestamp <= ts."""
        t, v = self._snapshot()
        i = bisect_right(t, ts) - 1
        return v[i] if i >= 0 else None

    def log_return(self, now: float, horizon_s: float) -> Optional[float]:
        t, v = self._snapshot()
        if not t:
            return None
        i_now = bisect_right(t, now) - 1
        i_then = bisect_right(t, now - horizon_s) - 1
        if i_then < 0 or i_now < 0 or i_then == i_now:
            return None
        return math.log(v[i_now] / v[i_then])

    def sigma_1s(self, now: float, lookback_s: float, step_s: float = 1.0) -> Optional[float]:
        """Desvio padrão do log-retorno normalizado para 1 segundo.
        step_s=1: amostras consecutivas (dt>1s normalizado por sqrt(dt)).
        step_s>1: retornos sobrepostos de `step_s` segundos, escalados por sqrt(step_s). A série
        Chainlink é suavizada no segundo a segundo, então a σ de 1 s subestima a variância em
        horizontes de minutos; 30 s aproxima melhor a σ de 5 min medida nas janelas resolvidas."""
        t, v = self._snapshot()
        i0 = bisect_left(t, now - lookback_s)
        xs: List[float] = []
        if step_s <= 1.0:
            for i in range(max(i0, 1), len(t)):
                dt = t[i] - t[i - 1]
                if dt <= 0 or dt > 30:
                    continue
                xs.append(math.log(v[i] / v[i - 1]) / math.sqrt(dt))
        else:
            j = i0
            for i in range(i0, len(t)):
                target = t[i] - step_s
                while j < i and t[j] < target:
                    j += 1
                if j >= i or j < i0:
                    continue
                dt = t[i] - t[j]
                if dt < step_s * 0.8 or dt > step_s * 1.5:
                    continue
                xs.append(math.log(v[i] / v[j]) / math.sqrt(dt))
        if len(xs) < 30:
            return None
        mu = sum(xs) / len(xs)
        var = sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)
        return math.sqrt(var)

    def samples_since(self, ts: float) -> List[Sample]:
        t, v = self._snapshot()
        i0 = bisect_left(t, ts)
        return list(zip(t[i0:], v[i0:]))

    def crossings(self, since_ts: float, level: float) -> int:
        """Quantas vezes (valor - level) trocou de sinal desde since_ts (empate conta como acima)."""
        s = self.samples_since(since_ts)
        n = 0
        prev = None
        for _, v in s:
            side = v >= level
            if prev is not None and side != prev:
                n += 1
            prev = side
        return n

    def extremes(self, since_ts: float) -> Optional[Tuple[float, float]]:
        s = self.samples_since(since_ts)
        if not s:
            return None
        vals = [v for _, v in s]
        return (min(vals), max(vals))


def seed_sigma_from_binance(client, limit: int = 1000) -> Optional[float]:
    """Vol por segundo a partir de klines de 1s da Binance. Só volatilidade: o nível Binance
    tem base de ~US$ 30 contra a Chainlink e nunca entra no modelo."""
    try:
        r = client.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1s", "limit": limit},
        )
        r.raise_for_status()
        closes = [float(k[4]) for k in r.json()]
    except Exception as e:
        log.warning("seed de sigma via Binance falhou: %s", e)
        return None
    if len(closes) < 60:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mu = sum(rets) / len(rets)
    var = sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var)


class ChainlinkFeed(threading.Thread):
    """Thread daemon que mantém o PriceBuffer alimentado; reconecta com backoff."""

    def __init__(self, buffer: PriceBuffer, url: str = WS_URL, symbol: str = "btc/usd"):
        super().__init__(name=f"chainlink-feed-{symbol.split('/')[0]}", daemon=True)
        self.buffer = buffer
        self.url = url
        self.symbol = symbol
        self._stop = threading.Event()
        self.connected = False
        self.reconnects = 0
        self.last_error: Optional[str] = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        import asyncio

        asyncio.run(self._loop())

    async def _loop(self) -> None:
        import asyncio

        import websockets

        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.url, additional_headers=WS_HEADERS, open_timeout=10, ping_interval=20
                ) as ws:
                    await ws.send(json.dumps(subscribe_msg(self.symbol)))
                    self.connected = True
                    backoff = 1.0
                    while not self._stop.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=15)
                        except asyncio.TimeoutError:
                            raise ConnectionError("sem mensagens por 15s")
                        if isinstance(msg, bytes):
                            msg = msg.decode("utf-8", "ignore")
                        samples = parse_frame(msg, self.symbol)
                        if samples:
                            self.buffer.add_many(samples)
            except Exception as e:
                self.connected = False
                self.reconnects += 1
                self.last_error = repr(e)
                log.warning("feed chainlink %s caiu (%s); reconectando em %.0fs", self.symbol, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

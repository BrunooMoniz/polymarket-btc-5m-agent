"""Vigia de carteira: confere o que o ledger diz contra o que a carteira tem de verdade.

  ledger  = PnL liquidado − custo das posições ainda abertas
  real    = colateral on-chain + valor das posições 5m ainda não resgatadas (data-api)
  desvio  = (real − ledger) − referência gravada na primeira leitura

Desvio acima do limite = dinheiro entrou/saiu sem o ledger saber (fill não registrado, depósito,
saque, resgate de posição de fora do motor): alerta e rebaseia. Também avisa quando o saldo livre
não paga uma ordem e há ganho parado esperando resgate (o motor não resgata sozinho)."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

log = logging.getLogger("wallet_watch")

COLLATERAL_TOKEN = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # py_clob_client_v2/config.py, Polygon, 6 casas
RPC_URLS = ("https://polygon-bor-rpc.publicnode.com", "https://polygon.gateway.tenderly.co")
POSITIONS_URL = "https://data-api.polymarket.com/positions"
UA = {"user-agent": "Mozilla/5.0 (X11; Linux x86_64) jev-5m-agent/1.0", "accept": "application/json"}
SLUG_PREFIX = "btc-updown-5m-"


def onchain_collateral(wallet: str, client: httpx.Client, rpcs=RPC_URLS) -> Optional[float]:
    data = "0x70a08231" + wallet.lower().replace("0x", "").rjust(64, "0")
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": COLLATERAL_TOKEN, "data": data}, "latest"]}
    for url in rpcs:
        try:
            res = client.post(url, json=body).json().get("result")
            if isinstance(res, str) and res.startswith("0x") and len(res) > 2:
                return int(res, 16) / 1e6
        except Exception as e:
            log.warning("rpc %s falhou: %s", url, type(e).__name__)
    return None


def positions_5m(wallet: str, client: httpx.Client) -> Optional[List[dict]]:
    try:
        out: List[dict] = []
        for page in range(10):  # posição perdida nunca some da lista: sem paginar, em ~2 semanas corta
            r = client.get(POSITIONS_URL, params={"user": wallet, "sizeThreshold": 0.1, "limit": 200, "offset": page * 200})
            r.raise_for_status()
            batch = r.json()
            out += [p for p in batch if str(p.get("slug") or "").startswith(SLUG_PREFIX)]
            if len(batch) < 200:
                return out
        return None  # passou de 2000 posições: leitura incompleta não serve de base
    except Exception as e:
        log.warning("data-api positions falhou: %s", type(e).__name__)
        return None


def won(position: dict) -> bool:
    """Posição vencedora: preço corrente acima de meio dólar (resolvido em 1) — não o flag redeemable,
    que também é True na perdedora."""
    return float(position.get("curPrice") or 0) > 0.5


@dataclass
class WalletReading:
    collateral: float
    positions_value: float   # valor corrente das posições 5m (abertas e vencedoras não resgatadas)
    redeemable_value: float  # parte já resolvida a nosso favor, parada até o resgate
    ledger_component: float

    @property
    def actual(self) -> float:
        return self.collateral + self.positions_value


class WalletWatch:
    def __init__(
        self,
        wallet: str,
        ledger: Any,
        notifier: Any,
        state_path: Path,
        divergence_usd: float = 1.0,
        min_order_usd: float = 5.0,
        interval_s: float = 300.0,
        client: Optional[httpx.Client] = None,
        clock: Callable[[], float] = time.time,
        collateral_fn: Optional[Callable[[], Optional[float]]] = None,
        positions_fn: Optional[Callable[[], Optional[List[dict]]]] = None,
    ):
        self.wallet = wallet
        self.ledger = ledger
        self.notifier = notifier
        self.state_path = Path(state_path)
        self.divergence_usd = divergence_usd
        self.min_order_usd = min_order_usd
        self.interval_s = interval_s
        self._clock = clock
        self._client = client or httpx.Client(headers=UA, timeout=8.0)
        self._collateral_fn = collateral_fn or (lambda: onchain_collateral(self.wallet, self._client))
        self._positions_fn = positions_fn or (lambda: positions_5m(self.wallet, self._client))
        self._pending: Optional[float] = None  # desvio visto na leitura anterior (exige 2 seguidas)

    # ------------------------------------------------------------------ leitura
    def read(self) -> Optional[WalletReading]:
        collateral = self._collateral_fn()
        positions = self._positions_fn()
        if collateral is None or positions is None:
            return None
        # Medido em 18/09/2026: ao resolver, a data-api zera currentValue de TODAS as posições, e marca
        # redeemable=True tanto na vencedora quanto na perdedora. Quem separa é curPrice (1 = venceu,
        # 0 = virou pó), e o valor da vencedora é size, porque cada share paga US$ 1.
        redeemable = sum(float(p.get("size") or 0) for p in positions if won(p))
        value = redeemable + sum(float(p.get("currentValue") or 0) for p in positions if not won(p))
        comp = self.ledger.realized_pnl_usd() - self.ledger.open_cost_usd()
        return WalletReading(collateral, value, redeemable, comp)

    def _load_ref(self) -> Optional[float]:
        try:
            return float(json.loads(self.state_path.read_text())["reference"])
        except Exception:
            return None

    def _save_ref(self, ref: float, why: str) -> None:
        self.state_path.write_text(json.dumps({"reference": round(ref, 6), "t": self._clock(), "why": why}))

    # ------------------------------------------------------------------ checagem
    def check(self) -> str:
        now = self._clock()
        rd = self.read()
        if rd is None:
            return "unavailable"

        if rd.collateral < self.min_order_usd and rd.redeemable_value >= 1.0:
            self.notifier.send(
                "wallet_locked",
                f"💤 Saldo livre US$ {rd.collateral:.2f} não paga uma ordem e há US$ {rd.redeemable_value:.2f} "
                "em posições ganhas esperando resgate. O motor não resgata sozinho: fica parado até o resgate.",
                3600,
            )
        elif rd.collateral < self.min_order_usd and self.ledger.open_cost_usd() == 0:
            self.notifier.send("wallet_empty", f"💤 Saldo livre US$ {rd.collateral:.2f}: não paga uma ordem. Motor parado por falta de saldo.", 3600)

        # Entre o fim da janela e a liquidação/resgate os dois lados andam em tempos diferentes:
        # só compara com o ledger em repouso (nada aberto, última mexida há > 3 min).
        live_orders = [r for r in self.ledger.open_order_rows() if now - float(r["ts"]) < 1200]
        if self.ledger.open_cost_usd() > 0 or live_orders or now - self.ledger.last_trade_update() < 180:
            self._pending = None
            return "busy"

        ref = self._load_ref()
        raw = rd.actual - rd.ledger_component
        if ref is None:
            self._save_ref(raw, "primeira leitura")
            self.ledger.journal("wallet_baseline", reference=raw, collateral=rd.collateral, positions=rd.positions_value)
            return "baseline"
        dev = raw - ref
        self.ledger.journal("wallet_check", collateral=rd.collateral, positions=rd.positions_value,
                            redeemable=rd.redeemable_value, ledger=rd.ledger_component, deviation=round(dev, 4))
        if abs(dev) <= self.divergence_usd:
            self._pending = None
            return "ok"
        if self._pending is None or abs(dev - self._pending) > 0.25:
            self._pending = dev  # primeira vez (ou valor ainda mexendo): confirma na próxima leitura
            return "pending"
        self._pending = None
        self.notifier.send(
            "wallet_divergence",
            f"⚠️ Carteira divergiu do ledger em US$ {dev:+.2f} (saldo livre {rd.collateral:.2f}, posições 5m "
            f"{rd.positions_value:.2f}, PnL do ledger {self.ledger.realized_pnl_usd():+.2f}). Causas usuais: fill não "
            "registrado, depósito/saque ou resgate de posição de fora do motor. Referência rebaseada.",
            0,
        )
        self.ledger.journal("wallet_divergence", deviation=round(dev, 4), collateral=rd.collateral,
                            positions=rd.positions_value, ledger=rd.ledger_component)
        self._save_ref(raw, f"rebase após desvio {dev:+.2f}")
        return "divergence"

    def start(self) -> None:
        threading.Thread(target=self._loop, name="wallet_watch", daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.check()
            except Exception:
                log.exception("falha na vigia de carteira")
            time.sleep(self.interval_s)


ACTIVITY_URL = "https://data-api.polymarket.com/activity"


def wallet_flow(wallet: str, since_ts: float, client: Optional[httpx.Client] = None,
                slug_prefix: str = SLUG_PREFIX) -> Optional[Dict[str, float]]:
    """Dinheiro que realmente entrou e saiu da carteira nos mercados do motor, desde since_ts, mais o
    que está vencido e ainda não resgatado. É a única leitura que não depende do nosso próprio ledger."""
    c = client or httpx.Client(headers=UA, timeout=15.0)
    try:
        acts: List[dict] = []
        for page in range(20):
            r = c.get(ACTIVITY_URL, params={"user": wallet, "limit": 500, "offset": page * 500})
            r.raise_for_status()
            batch = r.json()
            acts += batch
            if len(batch) < 500 or min((a.get("timestamp") or 0) for a in batch) < since_ts:
                break
        mine = [a for a in acts if (a.get("timestamp") or 0) >= since_ts
                and str(a.get("slug") or "").startswith(slug_prefix)]
        pos = positions_5m(wallet, c) or []
    except Exception as e:
        log.warning("fluxo da carteira indisponível: %s", type(e).__name__)
        return None
    usd = lambda a: float(a.get("usdcSize") or 0)
    pend = [p for p in pos if won(p)]
    return {
        "comprado": sum(usd(a) for a in mine if a.get("type") == "TRADE" and a.get("side") == "BUY"),
        "vendido": sum(usd(a) for a in mine if a.get("type") == "TRADE" and a.get("side") == "SELL"),
        "resgatado": sum(usd(a) for a in mine if a.get("type") == "REDEEM"),
        "pendente": sum(float(p.get("size") or 0) for p in pend),
        "pendentes": float(len(pend)),
    }


def resolutions(wallet: str, since_ts: float, client: Optional[httpx.Client] = None,
                slug_prefix: str = SLUG_PREFIX) -> Optional[Dict[str, Tuple[str, float]]]:
    """Quem ganhou de verdade, pelo dinheiro: slug -> ("won", payout) | ("lost", 0).

    O endpoint de preço erra o vencedor em janelas de empate quase perfeito (5 de 40 em 18/09/2026,
    uma delas por US$ 0,28 em 80 mil, sempre a nosso favor). O CTF não erra: resgate recebido é
    vitória, posição que sobrou valendo zero é derrota."""
    c = client or httpx.Client(headers=UA, timeout=15.0)
    out: Dict[str, Tuple[str, float]] = {}
    try:
        acts: List[dict] = []
        for page in range(20):
            r = c.get(ACTIVITY_URL, params={"user": wallet, "limit": 500, "offset": page * 500})
            r.raise_for_status()
            batch = r.json()
            acts += batch
            if len(batch) < 500 or min((a.get("timestamp") or 0) for a in batch) < since_ts:
                break
        for a in acts:
            slug = str(a.get("slug") or "")
            if a.get("type") != "REDEEM" or not slug.startswith(slug_prefix):
                continue
            pago = float(a.get("usdcSize") or 0)
            if pago > 0:
                anterior = out.get(slug, ("won", 0.0))[1]
                out[slug] = ("won", anterior + pago)
        for p_ in (positions_5m(wallet, c) or []):
            slug = str(p_.get("slug") or "")
            if slug in out:
                continue
            # "redeemable" também é True para posição perdedora: o preço é que separa ganhou de perdeu.
            out[slug] = ("won", float(p_.get("size") or 0)) if won(p_) else ("lost", 0.0)
    except Exception as e:
        log.warning("resolução pela carteira indisponível: %s", type(e).__name__)
        return None
    return out

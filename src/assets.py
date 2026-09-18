"""Um ativo por instância do motor. Tudo que muda entre BTC, ETH e SOL mora aqui.

σ e movimento típico de ETH e SOL foram medidos em 79 janelas resolvidas (18/09/2026, 19:40 UTC, trecho
agitado do dia). Os do BTC ficam nos valores em produção desde 17/09 de propósito: trocar o prior do
ativo que opera com dinheiro real é mudança de estratégia e tem shadow próprio (`sigma`) para provar.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class AssetSpec:
    name: str                  # chave curta (btc, eth, sol)
    symbol: str                # crypto-price: BTC | ETH | SOL
    feed_symbol: str           # WS Chainlink: btc/usd
    slug_prefix: str           # gamma: btc-updown-5m-
    label: str                 # como o Jev vê o ativo
    typical_abs_move_5m: float  # mediana de |close-open|, em dólares do ativo
    sigma_prior_1s: float


ASSETS: Dict[str, AssetSpec] = {
    "btc": AssetSpec("btc", "BTC", "btc/usd", "btc-updown-5m-", "BTC/USD", 36.6, 5.7e-5),
    "eth": AssetSpec("eth", "ETH", "eth/usd", "eth-updown-5m-", "ETH/USD", 3.98, 1.38e-4),
    "sol": AssetSpec("sol", "SOL", "sol/usd", "sol-updown-5m-", "SOL/USD", 0.17, 1.86e-4),
}


def spec_for(name: str) -> AssetSpec:
    key = (name or "btc").strip().lower()
    if key not in ASSETS:
        raise ValueError(f"ativo desconhecido: {name!r} (use {', '.join(sorted(ASSETS))})")
    return ASSETS[key]

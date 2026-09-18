"""Modelo de probabilidade em código. O Jev não precifica: ajusta o regime e veta.

P(Up) = Φ( d / (σ_usd · sqrt(τ)) ), com d = Chainlink_atual - PriceToBeat, τ = segundos restantes,
σ_usd = preço · σ_log_por_segundo · multiplicador_de_regime.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from src.polymarket_5m import Book, taker_fee_per_share


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_up(delta_usd: float, ref_price: float, sigma_1s: float, tau_s: float, sigma_mult: float = 1.0) -> float:
    if tau_s <= 0:
        return 1.0 if delta_usd >= 0 else 0.0
    sig_usd = ref_price * sigma_1s * max(sigma_mult, 1e-6) * math.sqrt(tau_s)
    if sig_usd <= 0:
        return 1.0 if delta_usd >= 0 else 0.0
    p = norm_cdf(delta_usd / sig_usd)
    return min(0.999, max(0.001, p))


def regime_multiplier(p_chop: float, p_trend: float) -> float:
    """Chop alarga σ (empurra p para 0,5 e derruba o edge); tendência limpa estreita um pouco.
    Distribuição espalhada (baixa confiança) fica perto de 1."""
    return min(1.6, max(0.8, 1.0 + 0.6 * p_chop - 0.25 * p_trend))


def maker_limit(book: Book, tick: float) -> Optional[float]:
    """Preço maker: um tick acima do melhor bid sem cruzar o ask. Sem os dois lados não cota."""
    if book.best_bid is None or book.best_ask is None:
        return None
    if book.best_ask - book.best_bid <= tick + 1e-9:
        limit = book.best_bid
    else:
        limit = book.best_bid + tick
    limit = round(limit / tick) * tick
    limit = round(limit, 4)
    if limit <= 0 or limit >= book.best_ask - 1e-9:
        return None
    return limit


@dataclass(frozen=True)
class Candidate:
    side: str            # "Up" | "Down"
    token_id: str
    limit_price: float   # preço maker
    p_side: float        # probabilidade do modelo para este lado
    edge_maker: float    # p_side - limit (taxa maker = 0)
    edge_taker: float    # p_side - ask - taxa taker (referência, não usado para entrar)
    best_bid: float
    best_ask: float


def best_candidate(
    p_up_model: float,
    book_up: Optional[Book],
    book_down: Optional[Book],
    token_up: str,
    token_down: str,
    tick: float,
    fee_rate: float,
) -> Optional[Candidate]:
    cands = []
    for side, book, token, p_side in (
        ("Up", book_up, token_up, p_up_model),
        ("Down", book_down, token_down, 1.0 - p_up_model),
    ):
        if book is None:
            continue
        limit = maker_limit(book, tick)
        if limit is None:
            continue
        cands.append(
            Candidate(
                side=side,
                token_id=token,
                limit_price=limit,
                p_side=p_side,
                edge_maker=p_side - limit,
                edge_taker=p_side - book.best_ask - taker_fee_per_share(book.best_ask, fee_rate),
                best_bid=book.best_bid,
                best_ask=book.best_ask,
            )
        )
    if not cands:
        return None
    return max(cands, key=lambda c: c.edge_maker)


def shares_for(stake_usd: float, price: float, min_shares: float, min_notional_usd: float) -> Optional[float]:
    """Quantidade (2 casas) para o stake; respeita mínimo de shares e notional. None se não cabe."""
    if price <= 0 or stake_usd <= 0:
        return None
    shares = math.floor(stake_usd / price * 100) / 100.0
    shares = max(shares, min_shares)
    if shares * price < min_notional_usd:
        shares = math.ceil(min_notional_usd / price * 100) / 100.0
    if shares * price > stake_usd + 1e-9:
        return None
    return shares


def brier(p: float, outcome_is_yes: bool) -> float:
    return (p - (1.0 if outcome_is_yes else 0.0)) ** 2


@dataclass(frozen=True)
class Entry:
    """Como entrar: 'maker' cota um tick acima do bid (taxa zero) e espera; 'taker' come o ask e paga
    a taxa, só quando o edge sobra muito sobre ela."""

    kind: str          # "maker" | "taker"
    price: float
    edge: float


def entry_for(cand: Candidate, min_net_edge: float, allow_taker: bool, taker_min_edge: float,
              maker_fill_rate: float = 1.0) -> Optional[Entry]:
    """Maker rende mais por share, mas só às vezes executa; taker rende menos e executa sempre. O que
    importa é o valor esperado por JANELA: taxa_de_fill x edge_maker contra edge_taker.

    Sem isso o caminho taker era inalcançável por construção: o limite maker nunca passa do ask, então
    edge_maker > edge_taker sempre, e com taker_min_edge acima do mínimo do maker o maker ganhava todas.
    Medido em 18/09/2026: fill de 50%, edge maker mediano 0,115 contra 0,091 do taker — 0,058 contra 0,091
    a favor do taker."""
    maker_ok = cand.edge_maker >= min_net_edge
    if not (allow_taker and cand.edge_taker >= taker_min_edge):
        return Entry("maker", cand.limit_price, cand.edge_maker) if maker_ok else None
    maker_ev = maker_fill_rate * cand.edge_maker if maker_ok else 0.0
    if cand.edge_taker > maker_ev:
        return Entry("taker", cand.best_ask, cand.edge_taker)
    return Entry("maker", cand.limit_price, cand.edge_maker) if maker_ok else None


def stake_for(edge: float, mode: str, min_stake: float, max_stake: float, ref_edge: float = 0.12,
              reliability: Optional[float] = None) -> float:
    """'fixed' aposta sempre o teto. 'conviction' cresce do piso ao teto entre 0 e ref_edge. 'jev'
    multiplica a fração do edge pela confiança que o Jev dá à estimativa do modelo naquela janela
    (sem resposta do Jev, cai no piso: tamanho grande exige julgamento explícito)."""
    if mode == "fixed" or ref_edge <= 0:
        return max_stake
    frac = min(1.0, max(0.0, edge / ref_edge))
    if mode == "jev":
        frac *= 0.0 if reliability is None else min(1.0, max(0.0, reliability))
    return round(min_stake + (max_stake - min_stake) * frac, 2)


def sigma_prior_from_windows(rows, configured: float, min_windows: int, max_drift: float) -> Optional[float]:
    """σ_1s da série real acumulada (|log(close/strike)| de janelas resolvidas), presa a ±max_drift do
    prior configurado. None enquanto não houver janelas suficientes."""
    # Só janelas cujo close é mesmo o BTC: uma linha com preço de token (0,30) viraria log-retorno de
    # -12 e estouraria a variância da série inteira.
    rets = [math.log(r["close_price"] / r["strike"]) for r in rows
            if r.get("strike") and r.get("close_price") and r["strike"] > 0 and r["close_price"] > 0
            and 0.5 < (r["close_price"] / r["strike"]) < 2.0]
    if len(rets) < min_windows:
        return None
    sigma_5m = math.sqrt(sum(x * x for x in rets) / len(rets))
    learned = sigma_5m / math.sqrt(300.0)
    return min(configured * (1 + max_drift), max(configured * (1 - max_drift), learned))

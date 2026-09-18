"""Jev (TypeSafe System One) como portão de bom senso, não como precificador.

Uma chamada por ponto de decisão, com estado SEM odds, SEM edge e SEM preço do mercado
(ancorar no preço destrói a independência que o cálculo de edge pressupõe). Três perguntas:
regime (Score), janela anômala (Noul) e direção pela regra exata de resolução (Noul).
A política (multiplicador de σ, veto) fica em código."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("jev_5m")

REGIME_LEVELS = [
    (
        "Choppy or mean-reverting window: the Chainlink price has crossed the Price to Beat "
        "several times, recent returns over 30s, 60s and 180s alternate in sign, and the current "
        "distance from the Price to Beat is small compared with the typical 5-minute move."
    ),
    (
        "Mixed or unclear window: the price sits near the Price to Beat with one or two crossings, "
        "or the returns over different horizons disagree, and the move is neither clearly trending "
        "nor clearly reversing."
    ),
    (
        "Clean directional trend: the price has moved away from the Price to Beat with no or one "
        "crossing, returns over 30s, 60s and 180s share the same sign, and the current distance is "
        "large compared with the typical 5-minute move."
    ),
]

ANOMALY_INSTRUCTIONS = (
    "Is this window anomalous for a statistical price model: a sudden spike or gap in the "
    "Chainlink series, a stale or frozen feed (old sample age or very few samples), or realized "
    "volatility far outside the typical range for this asset?"
)

DIRECTION_INSTRUCTIONS = (
    "At the end of this 5-minute window, will the Chainlink BTC/USD price be greater than or equal "
    "to the opening Price to Beat, so that the market resolves Up? A tie resolves Up."
)

# Conjunto "meta": em vez de pedir a direção (que a fórmula responde melhor: em 99 janelas a direção
# do Jev ficou em Brier 0,247 contra 0,194 do modelo, e não acrescenta nada além dele), pergunta-se
# se a ESTIMATIVA do modelo é confiável nesta janela. Isso é julgamento, não aritmética, e serve de
# base para o tamanho da aposta.
RELIABILITY_INSTRUCTIONS = (
    "The code model estimates the probability of this market resolving Up from a single idea: the "
    "current distance to the Price to Beat, divided by the volatility over the time left, read on a "
    "normal curve. Its estimate for this window is given as model_p_up in the state. Judging only "
    "from the path and the feed described, is that estimate trustworthy for this particular window? "
    "Answer yes when the path looks like ordinary diffusion the model can price: no gaps, a healthy "
    "feed, volatility in its usual range, and no pattern the model cannot see (a steady one-way "
    "drift, a price pinned to the Price to Beat, or repeated rejections at the same level). Answer "
    "no when something in the path would make a diffusion model wrong here."
)


@dataclass
class JevVerdict:
    regime_score: float
    regime_probs: Dict[int, float]
    regime_confidence: float
    anomaly_p: float
    direction_p_up: float
    reliability_p: Optional[float] = None   # 1 = a estimativa do modelo serve nesta janela
    latency_ms: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def p_chop(self) -> float:
        return float(self.regime_probs.get(0, 0.0))

    @property
    def p_trend(self) -> float:
        return float(self.regime_probs.get(2, 0.0))


def build_state(
    *,
    ts_start: int,
    ts_end: int,
    now: float,
    price_to_beat: float,
    chainlink_now: float,
    sample_age_s: float,
    samples_last_60s: int,
    crossings: int,
    window_low: Optional[float],
    window_high: Optional[float],
    returns_pct: Dict[str, Optional[float]],
    sigma_5m_usd: float,
    sigma_ratio_5m_vs_15m: Optional[float],
    typical_abs_move_5m_usd: float,
    model_p_up: Optional[float] = None,
) -> Dict[str, Any]:
    delta = chainlink_now - price_to_beat
    pos_in_range = None
    if window_low is not None and window_high is not None and window_high > window_low:
        pos_in_range = round((chainlink_now - window_low) / (window_high - window_low) * 100, 1)
    dt_now = datetime.fromtimestamp(now, timezone.utc)
    return {
        "market": {
            "asset": "BTC/USD",
            "venue": "Polymarket 5-minute Up/Down market",
            "resolution_rule": (
                "Resolves Up if the Chainlink BTC/USD price at the end of the 5-minute window is greater "
                "than or equal to the opening Price to Beat; otherwise Down. A tie resolves Up."
            ),
            "window_start_utc": datetime.fromtimestamp(ts_start, timezone.utc).strftime("%H:%M:%S"),
            "window_end_utc": datetime.fromtimestamp(ts_end, timezone.utc).strftime("%H:%M:%S"),
            "seconds_elapsed": int(now - ts_start),
            "seconds_remaining": int(ts_end - now),
        },
        "price_to_beat_usd": round(price_to_beat, 2),
        "chainlink_now_usd": round(chainlink_now, 2),
        "distance_from_price_to_beat": {
            "usd": round(delta, 2),
            "pct": round(delta / price_to_beat * 100, 4),
            "side_now": "above_or_equal" if delta >= 0 else "below",
        },
        "path_this_window": {
            "crossings_of_price_to_beat": crossings,
            "low_usd": round(window_low, 2) if window_low is not None else None,
            "high_usd": round(window_high, 2) if window_high is not None else None,
            "current_position_in_window_range_pct": pos_in_range,
        },
        "returns_pct": {k: (round(v, 4) if v is not None else None) for k, v in returns_pct.items()},
        "volatility": {
            "realized_sigma_5m_usd": round(sigma_5m_usd, 2),
            "sigma_ratio_last5m_vs_last15m": round(sigma_ratio_5m_vs_15m, 3) if sigma_ratio_5m_vs_15m else None,
            "typical_abs_move_5m_usd": round(typical_abs_move_5m_usd, 2),
        },
        "feed": {"sample_age_s": round(sample_age_s, 1), "samples_last_60s": samples_last_60s},
        "clock": {"time_utc": dt_now.strftime("%H:%M"), "weekday": dt_now.strftime("%A")},
        **({"model_p_up": round(model_p_up, 3)} if model_p_up is not None else {}),
    }


def questions(question_set: str = "direction") -> Dict[str, Any]:
    if question_set == "meta":
        return questions_meta()
    from typesafe_sdk import Noul, Score

    return {
        "regime": Score(
            instructions=(
                "Which situation best describes the price path of this window so far, judged only "
                "from the state provided?"
            ),
            criteria=REGIME_LEVELS,
        ),
        "anomaly": Noul(instructions=ANOMALY_INSTRUCTIONS),
        "direction_up": Noul(instructions=DIRECTION_INSTRUCTIONS),
    }


def questions_meta() -> Dict[str, Any]:
    """Mesmo custo de uma chamada: regime, anomalia e confiabilidade da estimativa do modelo."""
    from typesafe_sdk import Noul, Score

    q = questions()
    q.pop("direction_up")
    q["reliability"] = Noul(instructions=RELIABILITY_INSTRUCTIONS)
    return q


def _probs_by_int(probs: Any, n_levels: int = len(REGIME_LEVELS)) -> Dict[int, float]:
    """SDK 0.6 devolve níveis como int a partir de 0. Aceita strings e, se vier base 1
    (chaves 1..n sem 0), desloca para base 0 em vez de silenciar o regime."""
    out: Dict[int, float] = {}
    if isinstance(probs, dict):
        for k, v in probs.items():
            try:
                out[int(k)] = float(v)
            except Exception:
                continue
    if out and 0 not in out and set(out) == set(range(1, n_levels + 1)):
        out = {k - 1: v for k, v in out.items()}
    return out


def parse_response(resp: Any, latency_ms: int = 0) -> JevVerdict:
    score = resp.scores["regime"]
    anomaly = resp.nouls["anomaly"]
    reliability = resp.nouls.get("reliability")
    # Sem a pergunta de direção (conjunto "meta") o campo fica em 0,5: nada a dizer sobre o lado.
    direction = resp.nouls.get("direction_up")
    probs = _probs_by_int(getattr(score, "probabilities", {}))
    return JevVerdict(
        regime_score=float(score.score),
        regime_probs=probs,
        regime_confidence=float(getattr(score, "confidence", 0.0) or 0.0),
        anomaly_p=float(anomaly.noul),
        direction_p_up=float(direction.noul) if direction is not None else 0.5,
        reliability_p=float(reliability.noul) if reliability is not None else None,
        latency_ms=latency_ms,
        raw={
            "regime_score": float(score.score),
            "regime_probs": probs,
            "regime_confidence": float(getattr(score, "confidence", 0.0) or 0.0),
            "anomaly_p": float(anomaly.noul),
            "direction_p_up": float(direction.noul) if direction is not None else None,
            "reliability_p": float(reliability.noul) if reliability is not None else None,
        },
    )


class JevGate:
    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_s: float = 4.0,
        client_factory: Optional[Callable[[], Any]] = None,
        question_set: str = "direction",
    ):
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.question_set = question_set
        self._client_factory = client_factory
        self._client = None

    def _client_or_new(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                from typesafe_sdk import RetryPolicy, TypeSafeClient

                self._client = TypeSafeClient(
                    api_key=self.api_key,
                    retry=RetryPolicy(max_retries=1, backoff_max=0.3, timeout=self.timeout_s),
                )
        return self._client

    def evaluate(self, state: Dict[str, Any]) -> JevVerdict:
        t0 = time.time()
        resp = self._client_or_new().system_one(state=state, questions=questions(self.question_set))
        return parse_response(resp, latency_ms=int((time.time() - t0) * 1000))


def veto(verdict: JevVerdict, side: str, anomaly_max: float, min_side_p: float,
         min_reliability: float = 0.0) -> Optional[str]:
    """Devolve o motivo do veto ou None. Política explícita, em código."""
    if verdict.anomaly_p > anomaly_max:
        return f"anomalia {verdict.anomaly_p:.2f} > {anomaly_max:.2f}"
    if verdict.reliability_p is not None:
        if verdict.reliability_p < min_reliability:
            return f"Jev dá {verdict.reliability_p:.2f} de confiança ao modelo (< {min_reliability:.2f})"
        return None  # conjunto "meta": não existe pergunta de direção para vetar o lado
    p_side = verdict.direction_p_up if side == "Up" else 1.0 - verdict.direction_p_up
    if p_side < min_side_p:
        return f"Jev dá {p_side:.2f} para {side} (< {min_side_p:.2f})"
    return None

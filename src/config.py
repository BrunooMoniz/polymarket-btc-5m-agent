"""Configuração do motor 5m. Tudo vem do ambiente; default é PAPER (nunca live por omissão)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional


def _f(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    return float(raw) if raw not in (None, "") else default


def _i(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    return int(raw) if raw not in (None, "") else default


def _b(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def shadow_env(env: Mapping[str, str], name: str) -> dict:
    """Ambiente de um shadow: o do live, com SHADOW_<NOME>_* por cima, sempre PAPER, sem chave
    de carteira e com diretório próprio. Um shadow nunca herda o modo live."""
    prefix = f"SHADOW_{name.upper()}_"
    out = {k: v for k, v in env.items() if not k.startswith("SHADOW_")}
    out.update({k[len(prefix):]: v for k, v in env.items() if k.startswith(prefix)})
    out["EXECUTION_MODE"] = "paper"
    out.pop("POLYMARKET_PRIVATE_KEY", None)
    if not env.get(prefix + "DATA_DIR"):
        out["DATA_DIR"] = f"data-shadow-{name.lower()}"
    return out


def shadow_names(env: Mapping[str, str]) -> list:
    return [n.strip().lower() for n in (env.get("SHADOW_PROFILES") or "").split(",") if n.strip()]


@dataclass(frozen=True)
class Settings:
    execution_mode: str = "paper"              # paper | live
    data_dir: Path = field(default_factory=lambda: Path("data"))
    paper_bankroll_usd: float = 25.0
    max_stake_usd: float = 5.0
    min_shares: float = 5.0
    min_notional_usd: float = 1.0
    min_net_edge: float = 0.04
    daily_loss_limit_usd: float = 10.0
    skip_start_s: int = 30
    skip_end_s: int = 25
    eval_interval_s: float = 2.0
    order_ttl_s: float = 12.0
    max_requotes: int = 3
    max_jev_calls_per_window: int = 2
    jev_min_interval_s: float = 45.0   # espaça as chamadas dentro da janela (mais informação na 2ª)
    # Só o lado que o modelo favorece (p >= 0,5). Cauda "barata" contra o sinal do delta foi o que
    # queimou a carteira em 17/09 e não tem evidência de edge; o sinal do delta acerta 69% aos 60 s.
    favored_side_only: bool = True
    # Portões do Jev, separáveis para A/B: gate = veto por direção/anomalia; regime = multiplicador de σ.
    # Com os dois desligados o motor nem chama o Jev (economiza ~0,7 s de latência por ordem).
    # "direction" = pergunta o lado (a fórmula responde melhor); "meta" = pergunta se a estimativa do
    # modelo é confiável nesta janela, que é julgamento e serve de base para o tamanho da aposta.
    jev_question_set: str = "direction"
    jev_min_reliability: float = 0.0     # veto no conjunto "meta" (0 = não veta)
    jev_gate: bool = True
    jev_regime_adjust: bool = True
    anomaly_max: float = 0.5
    jev_min_side_p: float = 0.45
    feed_stale_s: float = 5.0
    sigma_lookback_s: int = 1800
    sigma_step_s: float = 30.0
    # σ_1s equivalente à σ de 5 min medida em 287 janelas resolvidas (17-18/09/2026): 0,00099/√300.
    sigma_prior_1s: float = 5.7e-5
    sigma_floor_ratio: float = 0.75   # σ usada nunca abaixo de 0,75 × prior (feed suavizado subestima)
    sigma_cap_ratio: float = 4.0      # nem acima de 4 × prior
    jev_timeout_s: float = 4.0
    fee_rate: float = 0.07
    # Rejeição "post-only cruza o book" não é recotação (a ordem nunca existiu): orçamento próprio.
    # Entrada taker: proibida por default (a taxa taker come o edge). Liberada por flag quando o edge
    # for muito maior que a taxa; primeiro se prova no shadow, depois se decide no live.
    allow_taker: bool = False
    taker_min_edge: float = 0.10
    # Fração das ordens maker que executa, medida no journal. Entra na conta maker x taker.
    maker_fill_rate: float = 0.5
    # Saída antecipada pelo modelo (0 desliga): vende no bid quando o lado comprado desaba.
    early_exit_p: float = 0.0
    early_exit_min_phase_s: int = 60
    early_exit_min_proceeds_usd: float = 1.0
    # Sizing: "fixed" (US$ max_stake sempre) ou "conviction" (entre min_stake e max_stake por edge).
    sizing_mode: str = "fixed"
    min_stake_usd: float = 1.0
    # Prior de σ recalculado da série real acumulada (bounded), em vez da constante de 17-18/09.
    sigma_prior_auto: bool = False
    sigma_prior_min_windows: int = 100
    sigma_prior_max_drift: float = 0.5   # o prior aprendido nunca sai de ±50% do configurado
    requote_recheck_s: float = 1.5   # 2ª conferência dos trades antes de recotar (indexação atrasa)
    max_cross_retries: int = 3
    outcome_backfill_per_sweep: int = 2   # resultados de janelas não apostadas, para calibração
    mark_interval_s: float = 20.0         # marcação a mercado da posição aberta (evidência p/ saída antecipada)
    egress_check_s: float = 300.0
    wallet_check_s: float = 300.0
    wallet_divergence_usd: float = 1.0
    notify_trades: bool = True
    typesafe_api_key: Optional[str] = None
    polymarket_private_key: Optional[str] = None
    proxy_wallet: Optional[str] = None
    chain_id: int = 137

    @property
    def kill_switch(self) -> Path:
        return self.data_dir / "KILL"

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.sqlite"

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.jsonl"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        mode = (env.get("EXECUTION_MODE") or "paper").strip().lower()
        if mode not in ("paper", "live"):
            raise ValueError(f"EXECUTION_MODE inválido: {mode!r} (use paper ou live)")
        sizing = (env.get("SIZING_MODE") or "fixed").strip().lower()
        if sizing not in ("fixed", "conviction", "jev"):
            raise ValueError(f"SIZING_MODE inválido: {sizing!r} (use fixed, conviction ou jev)")
        qset = (env.get("JEV_QUESTION_SET") or "direction").strip().lower()
        if qset not in ("direction", "meta"):
            raise ValueError(f"JEV_QUESTION_SET inválido: {qset!r} (use direction ou meta)")
        if sizing == "jev" and qset != "meta":
            raise ValueError("SIZING_MODE=jev exige JEV_QUESTION_SET=meta (o tamanho vem da confiabilidade)")
        return cls(
            execution_mode=mode,
            data_dir=Path(env.get("DATA_DIR") or "data"),
            paper_bankroll_usd=_f(env, "PAPER_BANKROLL_USD", 25.0),
            max_stake_usd=_f(env, "MAX_STAKE_USD", 5.0),
            min_shares=_f(env, "MIN_SHARES", 5.0),
            min_notional_usd=_f(env, "MIN_NOTIONAL_USD", 1.0),
            min_net_edge=_f(env, "MIN_NET_EDGE", 0.04),
            daily_loss_limit_usd=_f(env, "DAILY_LOSS_LIMIT_USD", 10.0),
            skip_start_s=_i(env, "WINDOW_SKIP_START_S", 30),
            skip_end_s=_i(env, "WINDOW_SKIP_END_S", 25),
            eval_interval_s=_f(env, "EVAL_INTERVAL_S", 2.0),
            order_ttl_s=_f(env, "ORDER_TTL_S", 12.0),
            max_requotes=_i(env, "MAX_REQUOTES", 3),
            max_jev_calls_per_window=_i(env, "MAX_JEV_CALLS_PER_WINDOW", 2),
            jev_min_interval_s=_f(env, "JEV_MIN_INTERVAL_S", 45.0),
            favored_side_only=_b(env, "FAVORED_SIDE_ONLY", True),
            jev_question_set=(env.get("JEV_QUESTION_SET") or "direction").strip().lower(),
            jev_min_reliability=_f(env, "JEV_MIN_RELIABILITY", 0.0),
            jev_gate=_b(env, "JEV_GATE", True),
            jev_regime_adjust=_b(env, "JEV_REGIME_ADJUST", True),
            anomaly_max=_f(env, "JEV_ANOMALY_MAX", 0.5),
            jev_min_side_p=_f(env, "JEV_MIN_SIDE_P", 0.45),
            feed_stale_s=_f(env, "FEED_STALE_S", 5.0),
            sigma_lookback_s=_i(env, "SIGMA_LOOKBACK_S", 1800),
            sigma_step_s=_f(env, "SIGMA_STEP_S", 30.0),
            sigma_prior_1s=_f(env, "SIGMA_PRIOR_1S", 5.7e-5),
            sigma_floor_ratio=_f(env, "SIGMA_FLOOR_RATIO", 0.75),
            sigma_cap_ratio=_f(env, "SIGMA_CAP_RATIO", 4.0),
            jev_timeout_s=_f(env, "JEV_TIMEOUT_S", 4.0),
            allow_taker=_b(env, "ALLOW_TAKER", False),
            taker_min_edge=_f(env, "TAKER_MIN_EDGE", 0.10),
            maker_fill_rate=_f(env, "MAKER_FILL_RATE", 0.5),
            early_exit_p=_f(env, "EARLY_EXIT_P", 0.0),
            early_exit_min_phase_s=_i(env, "EARLY_EXIT_MIN_PHASE_S", 60),
            early_exit_min_proceeds_usd=_f(env, "EARLY_EXIT_MIN_PROCEEDS_USD", 1.0),
            sizing_mode=(env.get("SIZING_MODE") or "fixed").strip().lower(),
            min_stake_usd=_f(env, "MIN_STAKE_USD", 1.0),
            sigma_prior_auto=_b(env, "SIGMA_PRIOR_AUTO", False),
            sigma_prior_min_windows=_i(env, "SIGMA_PRIOR_MIN_WINDOWS", 100),
            sigma_prior_max_drift=_f(env, "SIGMA_PRIOR_MAX_DRIFT", 0.5),
            requote_recheck_s=_f(env, "REQUOTE_RECHECK_S", 1.5),
            max_cross_retries=_i(env, "MAX_CROSS_RETRIES", 3),
            outcome_backfill_per_sweep=_i(env, "OUTCOME_BACKFILL_PER_SWEEP", 2),
            mark_interval_s=_f(env, "MARK_INTERVAL_S", 20.0),
            egress_check_s=_f(env, "EGRESS_CHECK_S", 300.0),
            wallet_check_s=_f(env, "WALLET_CHECK_S", 300.0),
            wallet_divergence_usd=_f(env, "WALLET_DIVERGENCE_USD", 1.0),
            notify_trades=_b(env, "NOTIFY_TRADES", True),
            typesafe_api_key=env.get("TYPESAFE_API_KEY") or None,
            polymarket_private_key=env.get("POLYMARKET_PRIVATE_KEY") or None,
            proxy_wallet=env.get("POLYMARKET_PROXY_WALLET") or None,
            chain_id=_i(env, "POLYGON_CHAIN_ID", 137),
        )

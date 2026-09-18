"""Daemon do motor 5m (systemd na VPS). Modo padrão PAPER; live só com EXECUTION_MODE=live
e chave explícita. Loga o modo de forma inequívoca no arranque."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import httpx

# O systemd já injeta o ambiente pelo EnvironmentFile; o .env só serve para rodar na mão.
# JEV_ENV_FILE isola um ensaio, para uma cópia não ler o .env de outra instalação.
for candidate in (Path(os.environ.get("JEV_ENV_FILE") or ".env"),):
    if candidate.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(candidate)
        except ImportError:
            pass
        break

from src import calibration, notify
from src.chainlink_feed import ChainlinkFeed, PriceBuffer, seed_sigma_from_binance
from src.config import Settings, shadow_env, shadow_names
from src.egress import EgressMonitor
from src.engine_5m import Engine
from src.jev_5m import JevGate
from src.ledger import Ledger
from src.polymarket_5m import HEADERS, PolymarketPublic
from src.shadow import CachedPM, SharedJevGate, start_shadow
from src.wallet_watch import WalletWatch

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("service_runner")


def _boot_notice(ledger: Ledger, notifier, text: str, every_s: float = 600.0) -> None:
    """Aviso de arranque com intervalo mínimo gravado em disco: um laço de reinício não vira rajada."""
    import time

    last = float(ledger.meta_get("boot_notice_t") or 0)
    if time.time() - last >= every_s:
        ledger.meta_set("boot_notice_t", str(time.time()))
        notifier.send("boot", text, 0)


def build_engine(settings: Settings) -> Engine:
    if not settings.typesafe_api_key:
        raise SystemExit("TYPESAFE_API_KEY ausente")
    pm = PolymarketPublic()
    buffer = PriceBuffer()
    feed = ChainlinkFeed(buffer)
    feed.start()
    ledger = Ledger(settings.ledger_path, settings.journal_path)
    # Um gate compartilhado POR conjunto de perguntas: motores que perguntam coisas diferentes não
    # podem reaproveitar o mesmo veredito.
    gates: dict = {}

    def gate_for(question_set: str) -> SharedJevGate:
        if question_set not in gates:
            gates[question_set] = SharedJevGate(JevGate(api_key=settings.typesafe_api_key,
                                                        timeout_s=settings.jev_timeout_s,
                                                        question_set=question_set))
        return gates[question_set]

    jev = gate_for(settings.jev_question_set)
    notifier = notify.from_env(os.environ, prefix=f"[JEV 5m {settings.execution_mode.upper()}] ")
    egress = None

    if settings.execution_mode == "live":
        from src.execution_5m import LiveBroker

        socks_proxy = os.environ.get("CLOB_SOCKS_PROXY") or None
        if not socks_proxy and os.environ.get("CLOB_ALLOW_DIRECT") != "1":
            raise SystemExit("MODO LIVE sem CLOB_SOCKS_PROXY: abortando (fail-closed). "
                             "Defina o proxy ou CLOB_ALLOW_DIRECT=1 para aceitar direto.")
        # Saída ruim não derruba mais o processo (o systemd ficava em laço de reinício): o motor sobe
        # fechado, sem postar, e o vigia libera quando a saída estiver em jurisdição aceita.
        fallbacks = [x.strip() or None for x in (os.environ.get("CLOB_EGRESS_FALLBACKS") or "").split(",") if x.strip()]
        egress = EgressMonitor(socks_proxy, settings.egress_check_s, notifier, fallbacks=fallbacks)
        st = egress.check()
        log.warning("MODO LIVE: ordens reais | carteira %s | rotas %s | em uso %s | egress ok=%s (%s)",
                    settings.proxy_wallet, len(egress.candidates), egress.socks or "DIRETO", st.ok, st.reason)
        egress.start()
        broker = LiveBroker(settings.polymarket_private_key or "", settings.proxy_wallet, settings.chain_id,
                            socks_proxy=egress.socks)
        # Nenhuma ordem de processo anterior fica viva. Só tenta com a saída boa: com a rota fora,
        # isso construiria o cliente do CLOB e derrubaria o arranque.
        if egress.ok():
            try:
                broker.cancel_all()
            except Exception as e:
                log.warning("cancel_all no arranque falhou: %s", e)
                ledger.journal("boot_cancel_all_failed", error=repr(e))
        else:
            log.warning("arranque sem cancel_all: %s", egress.reason())
            ledger.journal("boot_cancel_all_skipped", reason=egress.reason())
        if settings.proxy_wallet:
            WalletWatch(
                settings.proxy_wallet, ledger, notifier, settings.data_dir / "wallet_reference.json",
                divergence_usd=settings.wallet_divergence_usd, min_order_usd=settings.min_shares * 0.5,
                interval_s=settings.wallet_check_s,
            ).start()
    else:
        from src.execution_5m import PaperBroker

        broker = PaperBroker(pm.book, settings.paper_bankroll_usd)
        log.info("MODO PAPER: nenhuma ordem real; banca simulada US$ %.2f", settings.paper_bankroll_usd)

    seed = seed_sigma_from_binance(httpx.Client(headers=HEADERS, timeout=4))
    log.info("sigma_1s semente (Binance, só volatilidade): %s", f"{seed:.2e}" if seed else "indisponível")

    shadows = start_shadows(pm, buffer, gate_for, seed)
    compare = {name: s_.data_dir for name, (s_, _) in shadows.items()}

    def summary_extra(day: str) -> str:
        out = ""
        for name, (_, shadow_ledger) in shadows.items():
            d = shadow_ledger.day_stats(day)
            out += f"\nshadow {name}: US$ {d['pnl']:+.2f} sobre {d['staked']:.2f} | acertos {d['wins']}/{d['settled']}"
        return out

    engine = Engine(settings, pm, buffer, jev, broker, ledger, seed_sigma_1s=seed, notifier=notifier, egress=egress,
                    summary_extra=summary_extra,
                    daily_report=lambda: calibration.render(settings.data_dir, compare, settings.sigma_prior_1s))
    flags = []
    if settings.allow_taker:
        flags.append(f"taker>={settings.taker_min_edge:.2f}")
    if settings.early_exit_p > 0:
        flags.append(f"saída<{settings.early_exit_p:.2f}")
    if settings.sizing_mode != "fixed":
        flags.append(f"sizing {settings.sizing_mode}")
    if not settings.favored_side_only:
        flags.append("cauda liberada")
    if settings.sigma_prior_auto:
        flags.append("σ prior auto")
    _boot_notice(ledger, notifier, f"🚀 Motor no ar | shadows: {', '.join(shadows) or 'nenhum'}"
                 + (f" | extras: {', '.join(flags)}" if flags else "")
                 + (f" | egress: {egress.reason()}" if egress else ""))
    return engine


def start_shadows(pm: PolymarketPublic, buffer: PriceBuffer, gate_for, seed) -> dict:
    """Um motor PAPER por nome em SHADOW_PROFILES, com SHADOW_<NOME>_* por cima do ambiente do live."""
    from src.execution_5m import PaperBroker

    out = {}
    cached = CachedPM(pm)
    for name in shadow_names(os.environ):
        try:
            s_ = Settings.from_env(shadow_env(os.environ, name))
            if s_.data_dir.resolve() == Settings.from_env().data_dir.resolve():
                raise ValueError("shadow não pode escrever no diretório do motor principal")
            shadow_ledger = Ledger(s_.ledger_path, s_.journal_path)
            eng = Engine(s_, cached, buffer, gate_for(s_.jev_question_set),
                         PaperBroker(cached.book, s_.paper_bankroll_usd), shadow_ledger, seed_sigma_1s=seed)
            start_shadow(name, eng)
            out[name] = (s_, shadow_ledger)
            log.info("shadow %s no ar (PAPER) | dados: %s | perguntas=%s | sizing=%s | gate=%s | min_edge=%.3f",
                     name, s_.data_dir, s_.jev_question_set, s_.sizing_mode, s_.jev_gate, s_.min_net_edge)
        except Exception:
            log.exception("shadow %s não subiu; o motor principal segue", name)
    return out


def main() -> None:
    settings = Settings.from_env()
    log.info("=" * 70)
    log.info(" JEV BTC 5m ENGINE | MODO: %s | dados: %s", settings.execution_mode.upper(), settings.data_dir)
    log.info("=" * 70)
    engine = build_engine(settings)
    engine.run_forever()


if __name__ == "__main__":
    main()

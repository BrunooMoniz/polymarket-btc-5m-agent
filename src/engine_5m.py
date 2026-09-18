"""Loop por janela do mercado btc-updown-5m.

Por iteração (a cada eval_interval_s):
  1. liquida janelas preenchidas já encerradas (crypto-price completed; fallback Gamma);
  2. fora da faixa operacional (primeiros skip_start_s / últimos skip_end_s) não faz nada;
  3. uma posição por janela, persistida: reinício não reentra;
  4. falha fechada: sem mercado, sem Price to Beat, feed velho ou book vazio => pula;
  5. p_modelo = Φ(...); só chama o Jev se houver candidato com edge maker >= min_net_edge;
  6. Jev: regime ajusta σ, anomalia/direção vetam; recalcula edge; ordem maker post_only com TTL,
     até max_requotes recotações enquanto o edge persistir.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from src.chainlink_feed import PriceBuffer
from src.config import Settings
from src.egress import redact
from src.execution_5m import order_error_kind
from src.jev_5m import JevGate, JevVerdict, build_state, veto
from src.ledger import Ledger, day_of
from src.model import (Candidate, Entry, best_candidate, entry_for, maker_limit, p_up, regime_multiplier,
                       shares_for, sigma_prior_from_windows, stake_for)
from src.notify import NullNotifier
from src.polymarket_5m import (WINDOW_S, Market5m, PolymarketPublic, outcome_from_prices, taker_fee_per_share,
                               window_start)

log = logging.getLogger("engine_5m")


def _hm(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%H:%M")


class Engine:
    def __init__(
        self,
        settings: Settings,
        pm: PolymarketPublic,
        feed: PriceBuffer,
        jev: JevGate,
        broker: Any,
        ledger: Ledger,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        seed_sigma_1s: Optional[float] = None,
        typical_abs_move_5m_usd: Optional[float] = None,  # default: o do ativo (src/assets.py)
        notifier: Any = None,
        egress: Any = None,
        summary_extra: Optional[Callable[[str], str]] = None,
        daily_report: Optional[Callable[[], str]] = None,
        resolutions_fn: Optional[Callable[[float], Optional[Dict[str, Any]]]] = None,
    ):
        self.s = settings
        self.pm = pm
        self.feed = feed
        self.jev = jev
        self.broker = broker
        self.ledger = ledger
        self.clock = clock
        self.sleep = sleep
        self.seed_sigma_1s = seed_sigma_1s
        self.typical_abs_move = typical_abs_move_5m_usd if typical_abs_move_5m_usd is not None else settings.typical_abs_move_5m
        self._market_cache: Dict[int, Optional[Market5m]] = {}
        self._strike_cache: Dict[int, float] = {}
        self._jev_calls: Dict[int, int] = {}
        self._last_verdict: Dict[int, Any] = {}
        self._last_eval_journal: Dict[int, float] = {}
        self._last_skip_log: Dict[Any, float] = {}
        self._last_settle_check = 0.0
        self._cross_rejects: Dict[int, int] = {}
        self._outcome_attempts: Dict[int, int] = {}
        self._last_mark: Dict[int, float] = {}
        self._last_throttled: Dict[Any, float] = {}
        self._prior_checked_at = 0.0
        self._fill_rate_at = 0.0
        self._fill_rate_cached: Optional[float] = None
        self._prior_learned: Optional[float] = None
        self.notifier = notifier if notifier is not None else NullNotifier()
        self.egress = egress
        self.summary_extra = summary_extra
        self.daily_report = daily_report
        self.resolutions_fn = resolutions_fn      # verdade pelo dinheiro (só no live, com carteira)
        self._resolutions: Dict[str, Any] = {}
        self._resolutions_at = 0.0
        self.last_action = ""

    # ------------------------------------------------------------------ util
    def _market(self, ts: int) -> Optional[Market5m]:
        if ts not in self._market_cache or self._market_cache[ts] is None:
            self._market_cache[ts] = self.pm.market(ts)
        return self._market_cache[ts]

    def _strike(self, ts: int) -> Optional[float]:
        if ts in self._strike_cache:
            return self._strike_cache[ts]
        ptb = self.pm.price_to_beat(ts)
        if ptb is None or ptb.open_price is None:
            return None
        self._strike_cache[ts] = ptb.open_price
        return ptb.open_price

    def _prior_1s(self, now: float) -> float:
        """Prior configurado, ou o aprendido da série real (recalculado de hora em hora, bounded)."""
        if not self.s.sigma_prior_auto:
            return self.s.sigma_prior_1s
        if now - self._prior_checked_at >= 3600:
            self._prior_checked_at = now
            learned = sigma_prior_from_windows(self.ledger.all_rows(), self.s.sigma_prior_1s,
                                               self.s.sigma_prior_min_windows, self.s.sigma_prior_max_drift)
            if learned is not None and (self._prior_learned is None or abs(learned - self._prior_learned) > 1e-8):
                self.ledger.journal("sigma_prior", configured=self.s.sigma_prior_1s, learned=learned)
                log.info("prior de σ_1s atualizado pela série real: %.2e (configurado %.2e)", learned, self.s.sigma_prior_1s)
            self._prior_learned = learned
        return self._prior_learned if self._prior_learned is not None else self.s.sigma_prior_1s

    def _sigma_1s(self, now: float) -> Optional[float]:
        """σ realizada do feed (retornos de sigma_step_s, escalados) presa entre piso e teto
        relativos ao prior de 5 min. Sem feed suficiente usa a semente (Binance) ou o prior."""
        prior = self._prior_1s(now)
        sig = self.feed.sigma_1s(now, self.s.sigma_lookback_s, self.s.sigma_step_s)
        if sig is None:
            sig = self.seed_sigma_1s if self.seed_sigma_1s is not None else prior
        floor = prior * self.s.sigma_floor_ratio
        cap = prior * self.s.sigma_cap_ratio
        return min(cap, max(floor, sig))

    def _skip(self, ts: int, reason: str, final: bool = False, **extra: Any) -> str:
        self.ledger.upsert(ts, status="skipped" if final else "seen", reason=reason)
        key = (ts, reason)
        now = self.clock()
        if final or now - self._last_skip_log.get(key, 0.0) >= 20:
            self._last_skip_log[key] = now
            self.ledger.journal("skip", ts=ts, reason=reason, final=final, **extra)
            log.info("janela %s: %s%s", ts, reason, " (final)" if final else "")
        return f"skip:{reason}"

    def _apply_egress(self, ts: int) -> None:
        """Aplica a rota escolhida pelo vigia. Só aqui: neste ponto não existe ordem viva desta janela
        (o cliente do CLOB guarda um único http client; trocá-lo no meio de uma chamada a mataria)."""
        try:
            if self.broker.set_proxy(self.egress.desired_proxy()):
                self.ledger.journal("egress_applied", ts=ts, route=redact(self.egress.desired_proxy()))
        except Exception as e:
            self._journal_throttled("egress_apply_error", ts, 60, error=repr(e))

    def _entry(self, cand: Candidate) -> Optional[Entry]:
        return entry_for(cand, self.s.min_net_edge, self.s.allow_taker, self.s.taker_min_edge, self._fill_rate())

    def _fill_rate(self) -> float:
        """Taxa de fill do maker: a medida no próprio ledger quando há amostra, senão a configurada.
        Recalculada a cada 10 min; é ela que decide se vale esperar no book ou pagar a taxa."""
        if not self.s.allow_taker or not self.s.maker_fill_rate_auto:
            return self.s.maker_fill_rate
        now = self.clock()
        if now - self._fill_rate_at >= 600:
            self._fill_rate_at = now
            measured = self.ledger.maker_fill_rate()
            if measured is not None and measured != self._fill_rate_cached:
                self.ledger.journal("maker_fill_rate", measured=round(measured, 3), configured=self.s.maker_fill_rate)
            self._fill_rate_cached = measured
        return self._fill_rate_cached if self._fill_rate_cached is not None else self.s.maker_fill_rate

    def _collateral(self) -> float:
        return float(
            self.broker.collateral(
                open_cost_usd=self.ledger.open_cost_usd(),
                realized_pnl_usd=self.ledger.realized_pnl_usd(),
            )
        )

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        log.info("motor 5m %s iniciado | modo=%s | min_edge=%.3f | stake_max=%.2f", self.s.asset.upper(), self.s.execution_mode.upper(), self.s.min_net_edge, self.s.max_stake_usd)
        while True:
            try:
                self.last_action = self.step(self.clock())
            except Exception as e:
                log.exception("erro no passo do motor")
                self.notifier.send("engine_exception", f"🐞 Erro no passo do motor: {type(e).__name__}: {str(e)[:300]}")
            self.sleep(self.s.eval_interval_s)

    def step(self, now: float) -> str:
        ts = window_start(now)
        if self.egress is not None:
            # Topo do passo: aqui nunca há chamada ao CLOB em voo (o _place bloqueia até o fim do TTL
            # dentro de um passo), então é o único ponto seguro para trocar a rota.
            self._apply_egress(ts)
        swept = False
        if now - self._last_settle_check >= 10:
            self._last_settle_check = now
            swept = True
            self.resolve_open_orders(now)
            self.resolve_pending_exits(now)
            self.settle_pending(now)
            self.backfill_outcomes(now)
            self._daily_summary(now)

        phase = now - ts
        if phase < self.s.skip_start_s or phase > WINDOW_S - self.s.skip_end_s:
            return "outside"
        row0 = self.ledger.get(ts)
        if row0 and row0["status"] == "filled":
            return self._mark(ts, row0, now)
        if row0 and row0["status"] == "quoting":
            # Ordem desta janela com destino desconhecido (poll levantou ou o processo reiniciou no
            # meio do TTL): resolve antes de qualquer nova decisão, senão vira posição dupla.
            if swept:
                return "order_unresolved"  # a varredura deste passo já tentou
            res = self._resolve_order(row0, now)
            if res != "released":
                return res
        if self.ledger.is_final(ts):
            return "done"
        if self.s.kill_switch.exists():
            self.notifier.send("kill_switch", "🛑 Kill switch acionado: motor não abre posição até o arquivo KILL sair.", 6 * 3600)
            return self._skip(ts, "kill_switch", final=True)
        day_pnl = self.ledger.realized_pnl_usd(day_of(now))
        if day_pnl <= -abs(self.s.daily_loss_limit_usd):
            self.notifier.send(f"daily_stop:{day_of(now)}", f"🛑 Stop diário acionado: PnL do dia US$ {day_pnl:+.2f} (limite {-abs(self.s.daily_loss_limit_usd):.2f}). Volta à 00:00 UTC.", 86400)
            return self._skip(ts, f"stop diário: pnl {day_pnl:.2f}", final=True)

        market = self._market(ts)
        if market is None:
            return self._skip(ts, "mercado não encontrado", final=False)
        if market.closed or not market.accepting_orders:
            return self._skip(ts, "mercado não aceita ordens", final=True)

        strike = self._strike(ts)
        if strike is None:
            return self._skip(ts, "price_to_beat indisponível")

        latest = self.feed.latest()
        if latest is None or (now - latest[0]) > self.s.feed_stale_s:
            age = None if latest is None else round(now - latest[0], 1)
            if age is None or age > 120:
                self.notifier.send("feed_stale", f"📡 Feed Chainlink parado (idade {age}s). Motor sem operar até voltar.", 1800)
            return self._skip(ts, f"feed chainlink velho (idade={age}s)")
        _, px = latest

        sigma = self._sigma_1s(now)
        if sigma is None:
            return self._skip(ts, "sem sigma (feed curto e sem seed)")

        tau = market.ts_end - now
        delta = px - strike
        p_raw = p_up(delta, px, sigma, tau)

        book_up = self.pm.book(market.token_up)
        book_down = self.pm.book(market.token_down)
        cand = best_candidate(p_raw, book_up, book_down, market.token_up, market.token_down, market.tick, market.fee_rate)

        eval_rec = dict(
            ts=ts, phase=int(phase), strike=strike, px=px, delta=round(delta, 2), tau=int(tau),
            sigma_1s=sigma, sigma_feed_raw=self.feed.sigma_1s(now, self.s.sigma_lookback_s, self.s.sigma_step_s),
            p_raw=round(p_raw, 4),
            up_bid=book_up.best_bid if book_up else None, up_ask=book_up.best_ask if book_up else None,
            down_bid=book_down.best_bid if book_down else None, down_ask=book_down.best_ask if book_down else None,
            cand_side=cand.side if cand else None, cand_limit=cand.limit_price if cand else None,
            cand_edge=round(cand.edge_maker, 4) if cand else None,
            cand_edge_taker=round(cand.edge_taker, 4) if cand else None,
        )
        if now - self._last_eval_journal.get(ts, 0) >= 20:
            self._last_eval_journal[ts] = now
            self.ledger.journal("eval", **eval_rec)
            self.ledger.upsert(ts, slug=market.slug, mode=self.s.execution_mode, strike=strike, p_model=p_raw)

        if cand is None:
            return "no_book"
        if self._entry(cand) is None:
            return "no_edge"
        if self.s.favored_side_only and cand.p_side < 0.5:
            return "no_edge_favored"  # cauda contra o sinal do delta: fora por política

        if self.egress is not None and not self.egress.ok():
            return self._skip(ts, f"egress: {self.egress.reason()}")

        row = self.ledger.get(ts) or {}
        if (row.get("requotes") or 0) >= self.s.max_requotes:
            return self._skip(ts, "recotações esgotadas", final=True)
        # ---- Jev: regime + vetos ----------------------------------------
        # Um veredito vale por jev_min_interval_s (recotações e reavaliações reaproveitam);
        # passado o intervalo, nova chamada até o orçamento da janela. Com os dois portões
        # desligados não se chama o Jev: a ordem sai ~0,7 s antes.
        if not self.s.jev_gate and not self.s.jev_regime_adjust:
            verdict = None
        else:
            cached = self._last_verdict.get(ts)
            if cached is not None and now - cached[0] < self.s.jev_min_interval_s:
                verdict = cached[1]
            else:
                if self._jev_calls.get(ts, 0) >= self.s.max_jev_calls_per_window:
                    return self._skip(ts, "orçamento de chamadas ao Jev esgotado", final=True)
                state = self._jev_state(ts, market, now, strike, px, sigma, p_raw)
                self._jev_calls[ts] = self._jev_calls.get(ts, 0) + 1
                try:
                    verdict = self.jev.evaluate(state)
                except Exception as e:
                    self.ledger.journal("jev_error", ts=ts, error=repr(e))
                    return "jev_error"
                self._last_verdict[ts] = (now, verdict)
                self.ledger.upsert(ts, jev_calls=self._jev_calls[ts])
                if self.clock() - now > 3.0:
                    # Jev lento (ou fila atrás de um shadow): preço e book desta decisão envelheceram.
                    # O veredito fica guardado; o próximo passo decide com dados frescos.
                    return "jev_slow"

        mult = regime_multiplier(verdict.p_chop, verdict.p_trend) if (verdict and self.s.jev_regime_adjust) else 1.0
        p_adj = p_up(delta, px, sigma, tau, mult)
        cand2 = best_candidate(p_adj, book_up, book_down, market.token_up, market.token_down, market.tick, market.fee_rate)
        entry = self._entry(cand2) if cand2 else None
        reason = None
        if cand2 is None or entry is None:
            reason = f"edge após regime {(cand2.edge_maker if cand2 else float('nan')):.3f} < {self.s.min_net_edge:.3f}"
        elif self.s.favored_side_only and cand2.p_side < 0.5:
            reason = f"lado {cand2.side} não é o favorecido pelo modelo após regime (p={cand2.p_side:.2f})"
        elif verdict is not None and self.s.jev_gate:
            reason = veto(verdict, cand2.side, self.s.anomaly_max, self.s.jev_min_side_p, self.s.jev_min_reliability)

        self.ledger.journal(
            "decision", **eval_rec, jev=verdict.raw if verdict else None,
            jev_latency_ms=verdict.latency_ms if verdict else None, sigma_mult=round(mult, 3),
            p_adj=round(p_adj, 4), side=cand2.side if cand2 else None, limit=cand2.limit_price if cand2 else None,
            edge_adj=round(cand2.edge_maker, 4) if cand2 else None, vetoed=reason,
            entry_kind=entry.kind if entry else None, entry_edge=round(entry.edge, 4) if entry else None,
        )
        if reason:
            log.info("janela %s: sem ordem (%s)", ts, reason)
            return "vetoed"
        assert cand2 is not None and entry is not None
        return self._place(ts, market, cand2, p_adj, now, entry, verdict)

    # ------------------------------------------------------------------ ordem
    def _place(self, ts: int, market: Market5m, cand: Candidate, p_adj: float, now: float, entry: Entry,
               verdict: Any = None) -> str:
        # Reconfere o Price to Beat antes de arriscar dinheiro: se o valor consolidado mudou
        # desde a primeira leitura, descarta esta decisão e recalcula no próximo passo.
        fresh = self.pm.price_to_beat(ts)
        if fresh is None or fresh.open_price is None:
            return self._skip(ts, "price_to_beat indisponível na confirmação")
        cached = self._strike_cache.get(ts)
        if cached is not None and abs(fresh.open_price - cached) > 0.01:
            self.ledger.journal("strike_changed", ts=ts, old=cached, new=fresh.open_price)
            self._strike_cache[ts] = fresh.open_price
            self.ledger.upsert(ts, strike=fresh.open_price)
            return "strike_changed"

        collateral = self._collateral()  # no live é uma ida ao CLOB pelo proxy (1-3 s): antes do book, não depois
        reliability = getattr(verdict, "reliability_p", None)
        stake = min(stake_for(entry.edge, self.s.sizing_mode, self.s.min_stake_usd, self.s.max_stake_usd,
                              reliability=reliability), collateral)
        # Book fresco do lado escolhido: o da decisão já tem a latência do Jev (~0,7 s) e o post
        # ainda leva o tempo do proxy. Limite velho = "post-only cruza o book" (13 de 16 erros em 18/09).
        book = self.pm.book(cand.token_id)
        if book is None:
            return "no_book_at_post"
        if entry.kind == "taker":
            if book.best_ask is None:
                return "no_book_at_post"
            edge = cand.p_side - book.best_ask - taker_fee_per_share(book.best_ask, market.fee_rate)
            if edge < self.s.taker_min_edge:
                self._journal_throttled("edge_gone_at_post", ts, 10, kind="taker", ask=book.best_ask, edge=round(edge, 4))
                return "edge_gone_at_post"
            cand = replace(cand, limit_price=book.best_ask, edge_taker=edge, best_bid=book.best_bid, best_ask=book.best_ask)
        else:
            limit = maker_limit(book, market.tick)
            if limit is None:
                return "no_book_at_post"
            if abs(limit - cand.limit_price) > 1e-9:
                edge = cand.p_side - limit
                if edge < self.s.min_net_edge:
                    self._journal_throttled("edge_gone_at_post", ts, 10, old_limit=cand.limit_price, new_limit=limit, edge=round(edge, 4))
                    return "edge_gone_at_post"
                cand = replace(cand, limit_price=limit, edge_maker=edge, best_bid=book.best_bid, best_ask=book.best_ask)

        # O mercado tem mínimo de 5 shares: abaixo de min_shares x preço não existe ordem. Sizing
        # dinâmico sobe até esse piso quando ele cabe no teto; não cabendo, a janela fica de fora.
        min_shares = max(self.s.min_shares, market.min_size)
        floor_usd = min_shares * cand.limit_price
        if stake < floor_usd:
            if floor_usd > min(self.s.max_stake_usd, collateral) + 1e-9:
                reason = ("mínimo do mercado acima do teto" if floor_usd > self.s.max_stake_usd
                          else f"saldo insuficiente (colateral {collateral:.2f})")
                return self._skip(ts, f"{reason}: {min_shares:.0f} shares a {cand.limit_price:.2f} = {floor_usd:.2f}", final=True)
            self._journal_throttled("stake_raised_to_minimum", ts, 30, stake=round(stake, 2), floor=round(floor_usd, 2))
            stake = floor_usd
        shares = shares_for(stake, cand.limit_price, min_shares, self.s.min_notional_usd)
        if shares is None:
            return self._skip(ts, f"saldo insuficiente (colateral {collateral:.2f}, preço {cand.limit_price:.2f})", final=True)

        row = self.ledger.get(ts) or {}
        requotes = int(row.get("requotes") or 0)
        t_post = time.monotonic()
        if entry.kind == "taker":
            return self._place_taker(ts, cand, shares, p_adj, requotes, t_post)
        try:
            oid = self.broker.place_maker(cand.token_id, cand.limit_price, shares)
        except Exception as e:
            return self._order_error(ts, e, requotes, t_post, cand.limit_price)
        post_ms = int((time.monotonic() - t_post) * 1000)
        self.ledger.upsert(
            ts, status="quoting", side=cand.side, limit_price=cand.limit_price, shares=shares,
            order_id=oid, requotes=requotes + 1, p_model=p_adj, reason=None, entry_kind="maker",
        )
        self.ledger.journal("order_posted", ts=ts, order_id=oid, side=cand.side, limit=cand.limit_price, shares=shares,
                            p_adj=p_adj, post_ms=post_ms, phase=int(now - ts), stake=round(stake, 2),
                            reliability=reliability, mode=getattr(self.broker, "mode", "?"))
        log.info("janela %s: ordem %s %s x%.2f @ %.2f (p=%.3f, edge=%.3f, post %d ms)", ts, oid, cand.side, shares, cand.limit_price, p_adj, cand.edge_maker, post_ms)

        deadline = now + self.s.order_ttl_s
        st = None
        while True:
            try:
                st = self.broker.poll(oid)
            except Exception as e:  # rede instável: segue tentando até o TTL, nunca abandona a ordem viva
                self._journal_throttled("poll_error", ts, 5, order_id=oid, error=repr(e))
                st = None
            if st is not None and (st.filled > 0 or not st.open):
                break
            t = self.clock()
            if t >= deadline or t > market.ts_end - self.s.skip_end_s:
                break
            self.sleep(1.0)

        if st is None or st.open:
            ok = self.broker.cancel(oid)
            try:
                # resolve(), não poll(): ordem cancelada some do get_order, e um fill parcial entre a
                # última sondagem e o cancelamento só aparece nos trades.
                st = self.broker.resolve(oid, ts)
            except Exception as e:
                self.ledger.journal("poll_error", ts=ts, order_id=oid, error=repr(e))
                st = None
            # A ordem some do get_order assim que sai do book, e os trades podem demorar a aparecer:
            # antes de liberar recotação, confere uma segunda vez. Sem isto, um fill logo antes do
            # cancelamento viraria "morreu sem fill" e o motor postaria por cima (posição dupla).
            if st is not None and not st.open and st.filled <= 0 and st.status == "GONE" and ok:
                self.sleep(self.s.requote_recheck_s)
                try:
                    st = self.broker.resolve(oid, ts)
                except Exception as e:
                    self.ledger.journal("poll_error", ts=ts, order_id=oid, error=repr(e))
                    st = None
            # Ordem ainda aberta fecha a janela mesmo com fill parcial: gravar o parcial deixaria o
            # resto casável fora do ledger. Só se resolve quando o CLOB mostra a ordem morta.
            unconfirmed = st is not None and (st.open or (st.filled <= 0 and st.status == "GONE" and not ok))
            if st is None or unconfirmed:
                # Sem confirmação de que a ordem morreu: nunca recotar (evita posição dupla). A janela
                # fecha como "orphan" e resolve_open_orders descobre depois se houve fill.
                self.ledger.upsert(ts, status="orphan", reason=f"cancelamento não confirmado ({oid})")
                self.ledger.journal("cancel_unconfirmed", ts=ts, order_id=oid, status=getattr(st, "status", None))
                self.notifier.send("cancel_unconfirmed", f"⚠️ Cancelamento da ordem {oid[:14]}… não confirmado (janela {ts}). Janela fechada; o motor confere depois se executou.", 0)
                log.error("janela %s: cancelamento da ordem %s não confirmado; janela encerrada sem recotação", ts, oid)
                return "cancel_unconfirmed"

        if st is not None and st.filled > 0:
            return self._record_fill(ts, oid, cand.side, st, cand.limit_price)

        if requotes + 1 >= self.s.max_requotes:
            self.ledger.upsert(ts, status="unfilled", reason="sem fill após recotações")
            self.ledger.journal("unfilled", ts=ts, order_id=oid, requotes=requotes + 1)
            return "unfilled"
        self.ledger.upsert(ts, status="seen", reason="ordem cancelada por TTL; pode recotar")
        self.ledger.journal("cancel_ttl", ts=ts, order_id=oid, requotes=requotes + 1)
        return "requote"

    def _place_taker(self, ts: int, cand: Candidate, shares: float, p_adj: float, requotes: int, t_post: float) -> str:
        """Entrada taker (FOK): paga a taxa e não deixa ordem no book. Só chega aqui com ALLOW_TAKER e
        edge acima de taker_min_edge; uma tentativa por passo, com o mesmo teto de recotações."""
        try:
            st = self.broker.place_taker(cand.token_id, cand.limit_price, shares)
        except Exception as e:
            return self._order_error(ts, e, requotes, t_post, cand.limit_price)
        post_ms = int((time.monotonic() - t_post) * 1000)
        oid = st.order_id or f"taker-{ts}"
        self.ledger.upsert(ts, status="quoting", side=cand.side, limit_price=cand.limit_price, shares=shares,
                           order_id=oid, requotes=requotes + 1, p_model=p_adj, reason=None, entry_kind="taker")
        self.ledger.journal("order_posted", ts=ts, order_id=oid, side=cand.side, limit=cand.limit_price, shares=shares,
                            p_adj=p_adj, post_ms=post_ms, kind="taker", mode=getattr(self.broker, "mode", "?"))
        if st.filled <= 0 and st.status not in ("KILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"):
            # FOK sem "morreu" explícito: a indexação dos trades atrasa 1-3 s. Confere de novo antes de
            # concluir que não executou, senão o próximo passo compra por cima.
            self.sleep(self.s.requote_recheck_s)
            try:
                st = self.broker.resolve(oid, ts)
            except Exception as e:
                self.ledger.journal("poll_error", ts=ts, order_id=oid, error=repr(e))
                st = None
            if st is None or st.open or (st.filled <= 0 and st.status == "GONE"):
                self.ledger.upsert(ts, status="orphan", reason=f"taker sem destino confirmado ({oid})")
                self.ledger.journal("cancel_unconfirmed", ts=ts, order_id=oid, status=getattr(st, "status", None))
                return "cancel_unconfirmed"
        if st.filled > 0:
            return self._record_fill(ts, oid, cand.side, st, cand.limit_price)
        self.ledger.journal("taker_killed", ts=ts, order_id=oid, price=cand.limit_price)
        if requotes + 1 >= self.s.max_requotes:
            self.ledger.upsert(ts, status="unfilled", reason="taker não executou")
            return "unfilled"
        self.ledger.upsert(ts, status="seen", reason="taker não executou; pode tentar de novo")
        return "requote"

    def _order_error(self, ts: int, e: Exception, requotes: int, t_post: float, limit: float) -> str:
        kind = order_error_kind(e)
        self.ledger.journal("order_error", ts=ts, kind=kind, error=repr(e), limit=limit,
                            post_ms=int((time.monotonic() - t_post) * 1000))
        if kind == "cross":
            n = self._cross_rejects.get(ts, 0) + 1
            self._cross_rejects[ts] = n
            if n >= self.s.max_cross_retries:
                return self._skip(ts, "post-only cruzou o book em todas as tentativas", final=True)
            return "cross_retry"
        if kind == "region":
            if self.egress is not None:
                self.egress.mark_region_block()
            else:
                self.notifier.send("order_error:region", "⛔ CLOB recusou ordem por região (403).")
            return "order_error_region"
        if kind == "unknown":
            try:
                self.broker.cancel_all()
                swept = "cancel_all enviado"
            except Exception as e2:
                swept = f"cancel_all também falhou ({type(e2).__name__}): pode haver ordem viva até o fim da janela"
            self.ledger.journal("post_unknown", ts=ts, swept=swept)
            self.notifier.send("order_error:unknown", f"❗ Post de ordem sem resposta do CLOB ({_hm(ts)}): {str(e)[:200]}. {swept}. Janela encerrada.", 0)
            return self._skip(ts, f"post sem resposta do CLOB; {swept}", final=True)
        self.notifier.send("order_error:rejected", f"❗ CLOB recusou a ordem ({_hm(ts)}): {str(e)[:300]}")
        self.ledger.upsert(ts, status="seen", requotes=requotes + 1, reason=f"erro ao postar: {e}")
        return "order_error"

    def _record_fill(self, ts: int, oid: str, side: Optional[str], st: Any, limit_price: float, recovered: bool = False) -> str:
        fill_price = st.avg_price or limit_price
        cost = round(st.filled * fill_price, 4)
        self.ledger.upsert(ts, status="filled", filled_shares=st.filled, fill_price=fill_price, cost_usd=cost, reason=None)
        self.ledger.journal("fill", ts=ts, order_id=oid, side=side, filled=st.filled, price=fill_price, cost=cost, recovered=recovered)
        log.info("janela %s: FILL %s %.2f @ %.2f (custo %.2f)%s", ts, side, st.filled, fill_price, cost, " [recuperado]" if recovered else "")
        if self.s.notify_trades or recovered:
            extra = " (fill descoberto depois de cancelamento não confirmado)" if recovered else ""
            self.notifier.send(f"fill:{ts}", f"🎯 Fill {_hm(ts)} {side} {st.filled:.2f} @ {fill_price:.2f} (US$ {cost:.2f}){extra}", 0)
        return "filled"

    def _journal_throttled(self, event: str, ts: int, every_s: float, **data: Any) -> None:
        key = (event, ts)
        now = self.clock()
        if now - self._last_throttled.get(key, -1e18) >= every_s:
            self._last_throttled[key] = now
            self.ledger.journal(event, ts=ts, **data)

    # ------------------------------------------------------------------ ordens sem destino confirmado
    def _resolve_order(self, row: Dict[str, Any], now: float) -> str:
        """Descobre o destino de uma ordem 'quoting'/'orphan'. 'released' = morta sem fill e a janela
        ainda pode cotar; 'order_unresolved' = o CLOB não respondeu ou a ordem segue viva: ninguém posta
        nada nesta janela e a linha continua na fila."""
        ts, oid = int(row["ts"]), str(row["order_id"])
        market_dead = now > ts + WINDOW_S + 600  # mercado fechado há 10 min: não existe ordem viva
        if market_dead and now - self._last_throttled.get(("resolve", ts), -1e18) < 60:
            return "order_unresolved"  # linha antiga: uma tentativa por minuto basta
        self._last_throttled[("resolve", ts)] = now
        try:
            st = self.broker.resolve(oid, ts)
            if st.open and not market_dead:
                self.broker.cancel(oid)
                st = self.broker.resolve(oid, ts)
        except Exception as e:
            self._journal_throttled("poll_error", ts, 60, order_id=oid, error=repr(e))
            if now > ts + WINDOW_S + 900:
                self.notifier.send(f"unresolved:{ts}", f"⚠️ Ordem {oid[:14]}… da janela {_hm(ts)} segue sem destino confirmado (CLOB não responde). O ledger pode estar sem um fill.", 6 * 3600)
            return "order_unresolved"
        if st.open and not market_dead:
            # Viva (inteira ou com fill parcial) e o cancelamento não pegou: registrar o parcial agora
            # tiraria a linha da fila com o resto da ordem ainda casável.
            return "order_unresolved"
        if st.filled > 0:
            return self._record_fill(ts, oid, row.get("side"), st, float(row.get("limit_price") or 0), recovered=row["status"] == "orphan")
        if st.status == "GONE" and now < ts + WINDOW_S + 60:
            # Fora do book e sem trade listado: pode ser só atraso da API de trades logo após o match.
            # O "sem fill" só vale depois que a janela acabou; até lá ninguém posta nada nela.
            return "order_unresolved"
        if row["status"] == "quoting" and now <= ts + WINDOW_S - self.s.skip_end_s:
            self.ledger.upsert(ts, status="seen", reason="ordem anterior confirmada morta; pode recotar")
            self.ledger.journal("order_resolved", ts=ts, order_id=oid, result="released")
            return "released"
        self.ledger.upsert(ts, status="unfilled", reason="ordem confirmada morta sem fill")
        self.ledger.journal("order_resolved", ts=ts, order_id=oid, result="unfilled", clob_status=st.status)
        return "unfilled"

    def resolve_open_orders(self, now: float) -> int:
        n = 0
        for row in self.ledger.open_order_rows():
            if self._resolve_order(row, now) != "order_unresolved":
                n += 1
        return n

    # ------------------------------------------------------------------ evidência
    def _mark(self, ts: int, row: Dict[str, Any], now: float) -> str:
        """Marcação a mercado da posição aberta: quanto o modelo dá para o nosso lado e a que preço
        sairíamos agora. Só journal; é a evidência para decidir saída antecipada depois."""
        if now - self._last_mark.get(ts, 0.0) < self.s.mark_interval_s:
            return "holding"
        self._last_mark[ts] = now
        try:
            market, strike, latest = self._market(ts), self._strike_cache.get(ts) or row.get("strike"), self.feed.latest()
            if market is None or strike is None or latest is None or now - latest[0] > self.s.feed_stale_s:
                return "holding"
            px, sigma = latest[1], self._sigma_1s(now)
            p = p_up(px - float(strike), px, sigma, market.ts_end - now)
            side = row.get("side")
            token = market.token_up if side == "Up" else market.token_down
            book = self.pm.book(token)
            p_side = round(p if side == "Up" else 1 - p, 4)
            self.ledger.journal(
                "mark", ts=ts, phase=int(now - ts), side=side, p_side=p_side,
                bid=book.best_bid if book else None, ask=book.best_ask if book else None,
                fill_price=row.get("fill_price"), shares=row.get("filled_shares"), delta=round(px - float(strike), 2),
            )
            if self._should_exit(ts, row, p_side, book, now, market):
                return self._exit_position(ts, row, token, book, p_side, now)
        except Exception:
            log.exception("falha ao marcar posição da janela %s", ts)
        return "holding"

    def _should_exit(self, ts: int, row: Dict[str, Any], p_side: float, book: Any, now: float, market: Market5m) -> bool:
        """Saída antecipada (desligada por default): o modelo virou contra a posição e o bid ainda paga
        mais do que esperar até o fim valeria. Perto do fechamento não adianta vender: o mercado já sabe."""
        if self.s.early_exit_p <= 0 or book is None or book.best_bid is None:
            return False
        if row.get("exit_pending"):
            return False  # venda anterior sem destino confirmado: vender de novo venderia o que já foi
        if p_side >= self.s.early_exit_p:
            return False
        if now - ts < self.s.early_exit_min_phase_s or now > market.ts_end - self.s.skip_end_s:
            return False
        shares = float(row.get("filled_shares") or 0)
        return shares * book.best_bid >= self.s.early_exit_min_proceeds_usd

    def _exit_position(self, ts: int, row: Dict[str, Any], token: str, book: Any, p_side: float, now: float) -> str:
        shares, bid = float(row.get("filled_shares") or 0), float(book.best_bid)
        try:
            st = self.broker.sell_taker(token, bid, shares)
        except Exception as e:
            self._journal_throttled("exit_error", ts, 30, error=repr(e))
            return "holding"
        oid = st.order_id or f"exit-{ts}"
        if st.open or (st.filled <= 0 and st.status not in ("KILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED")):
            # Sem confirmação do destino da venda: congela a saída desta janela até a varredura
            # descobrir o que aconteceu. Repetir aqui venderia cotas que talvez já não existam.
            self.ledger.upsert(ts, exit_order_id=oid, exit_pending=1, reason="saída sem confirmação")
            self.ledger.journal("exit_unconfirmed", ts=ts, order_id=oid, status=st.status, bid=bid)
            self.notifier.send(f"exit_unconfirmed:{ts}", f"⚠️ Venda de saída antecipada {_hm(ts)} sem confirmação ({st.status or 'sem status'}). O motor não repete a venda; confere na varredura.", 0)
            return "holding"
        if st.filled <= 0:
            self._journal_throttled("exit_killed", ts, 30, bid=bid, p_side=p_side)
            return "holding"
        return self._book_exit(ts, row, oid, st, bid, p_side, now)

    def _book_exit(self, ts: int, row: Dict[str, Any], oid: str, st: Any, bid: float, p_side: Optional[float], now: float) -> str:
        """Contabiliza a venda (total ou parcial). Parcial realiza caixa agora (entra no stop diário) e
        deixa o resto liquidar no fim da janela."""
        shares = float(row.get("filled_shares") or 0)
        cost = float(row.get("cost_usd") or 0)
        price = st.avg_price or bid
        sold = min(st.filled, shares)
        proceeds = round(sold * price - taker_fee_per_share(price, self.s.fee_rate) * sold, 4)
        if sold < shares - 1e-6:
            cost_sold = round(cost * (sold / shares), 4) if shares > 0 else 0.0
            partial = round(float(row.get("partial_pnl_usd") or 0) + proceeds - cost_sold, 4)
            self.ledger.upsert(ts, filled_shares=round(shares - sold, 4), cost_usd=round(cost - cost_sold, 4),
                               partial_pnl_usd=partial, exit_pending=0, exit_order_id=None, reason=None)
            self.ledger.journal("exit_partial", ts=ts, order_id=oid, sold=sold, price=price, proceeds=proceeds,
                                cost=cost_sold, pnl=round(proceeds - cost_sold, 4), p_side=p_side)
            return "holding"
        pnl = round(proceeds - cost, 4)
        # close_price guarda o BTC no fim da janela (o backfill preenche depois); o preço do token vai
        # em exit_price, senão a série de σ e a calibração leem 0,30 como se fosse preço de BTC.
        self.ledger.upsert(ts, status="closed", pnl_usd=pnl, exit_price=price, outcome_source="early_exit",
                           reconciled=1, exit_pending=0, exit_order_id=oid, reason=None)
        self.ledger.journal("exit", ts=ts, order_id=oid, side=row.get("side"), shares=sold, price=price,
                            proceeds=proceeds, cost=cost, pnl=pnl, p_side=p_side, phase=int(now - ts))
        log.info("janela %s: SAÍDA antecipada %s %.2f @ %.2f (pnl %.2f)", ts, row.get("side"), sold, price, pnl)
        self.notifier.send(f"exit:{ts}", f"🚪 Saída antecipada {_hm(ts)} {row.get('side')} {sold:.2f} @ {price:.2f} | PnL US$ {pnl:+.2f}"
                           + (f" (modelo dava {p_side:.2f} ao lado)" if p_side is not None else ""), 0)
        return "closed"

    def resolve_pending_exits(self, now: float) -> int:
        """Descobre o destino de vendas de saída não confirmadas. Só libera nova tentativa quando o CLOB
        diz que a venda morreu sem executar."""
        n = 0
        for row in self.ledger.exit_pending_rows():
            ts, oid = int(row["ts"]), str(row["exit_order_id"])
            try:
                st = self.broker.resolve(oid, ts)
            except Exception as e:
                self._journal_throttled("exit_poll_error", ts, 60, order_id=oid, error=repr(e))
                continue
            if st.open:
                continue
            n += 1
            if st.filled > 0:
                self._book_exit(ts, row, oid, st, float(row.get("fill_price") or 0), None, now)
            else:
                self.ledger.upsert(ts, exit_pending=0, exit_order_id=None, reason=None)
                self.ledger.journal("exit_resolved", ts=ts, order_id=oid, result="sem execução")
        return n

    def backfill_outcomes(self, now: float) -> int:
        """Resultado das janelas não apostadas (crypto-price consolidado). Sem isso a calibração só
        enxerga as poucas janelas com fill."""
        n = 0
        for ts in self.ledger.missing_outcome(int(now) - WINDOW_S - 30, 500):
            if n >= self.s.outcome_backfill_per_sweep:
                break
            if self._outcome_attempts.get(ts, 0) >= 5:
                continue
            self._outcome_attempts[ts] = self._outcome_attempts.get(ts, 0) + 1
            n += 1
            ptb = self.pm.price_to_beat(ts)
            if ptb is None or not ptb.completed or ptb.open_price is None or ptb.close_price is None:
                continue
            outcome = outcome_from_prices(ptb.open_price, ptb.close_price)
            fields: Dict[str, Any] = dict(outcome=outcome, close_price=ptb.close_price)
            if (self.ledger.get(ts) or {}).get("strike") is None:
                fields["strike"] = ptb.open_price
            self.ledger.upsert(ts, **fields)
            self.ledger.journal("outcome", ts=ts, open=ptb.open_price, close=ptb.close_price, outcome=outcome)
        return n

    def _daily_summary(self, now: float) -> None:
        """Resumo do dia UTC anterior (o dia do ledger e do stop diário é UTC: vira às 21:00 de Brasília)."""
        today = day_of(now)
        last = self.ledger.meta_get("summary_day")
        if last == today:
            return
        if last is None:
            self.ledger.meta_set("summary_day", today)
            return
        if now - datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() < 180:
            return  # espera a última janela do dia liquidar
        self.ledger.meta_set("summary_day", today)
        self.notifier.send(f"summary:{last}", self.summary_text(last), 0)
        if self.daily_report is not None:
            try:
                self.notifier.send(f"calibration:{last}", "🔬 Calibração acumulada", 0, mono=self.daily_report())
            except Exception:
                log.exception("falha ao montar o relatório de calibração")

    def summary_text(self, day: str) -> str:
        d = self.ledger.day_stats(day)
        hit = f"{d['wins']}/{d['settled']}" if d["settled"] else "0/0"
        text = (
            f"📊 Dia {day} (UTC) | {self.s.execution_mode.upper()}\n"
            f"PnL do dia: US$ {d['pnl']:+.2f} sobre {d['staked']:.2f} apostados | acertos {hit}\n"
            f"Janelas vistas: {d['windows']} | em aberto: {d['open']}\n"
            f"PnL acumulado: US$ {self.ledger.realized_pnl_usd():+.2f}"
        )
        if self.summary_extra is not None:
            try:
                text += self.summary_extra(day)
            except Exception:
                log.exception("falha no complemento do resumo diário")
        return text

    # ------------------------------------------------------------------ Jev
    def _jev_state(self, ts: int, market: Market5m, now: float, strike: float, px: float, sigma: float,
                   model_p_up: Optional[float] = None) -> Dict[str, Any]:
        ext = self.feed.extremes(ts)
        sig5 = self.feed.sigma_1s(now, 300)
        sig15 = self.feed.sigma_1s(now, 900)
        ratio = (sig5 / sig15) if (sig5 and sig15) else None
        returns = {}
        for name, h in (("30s", 30), ("60s", 60), ("180s", 180), ("300s", 300), ("900s", 900)):
            r = self.feed.log_return(now, h)
            returns[name] = (r * 100) if r is not None else None
        samples60 = len(self.feed.samples_since(now - 60))
        latest = self.feed.latest()
        return build_state(
            ts_start=ts, ts_end=market.ts_end, now=now, price_to_beat=strike, chainlink_now=px,
            sample_age_s=(now - latest[0]) if latest else 999.0, samples_last_60s=samples60,
            crossings=self.feed.crossings(ts, strike), window_low=ext[0] if ext else None,
            window_high=ext[1] if ext else None, returns_pct=returns,
            sigma_5m_usd=px * sigma * math.sqrt(300), sigma_ratio_5m_vs_15m=ratio,
            typical_abs_move_5m_usd=self.typical_abs_move,
            model_p_up=model_p_up if self.s.jev_question_set == "meta" else None,
            asset_label=self.s.spec.label,
        )

    # ------------------------------------------------------------------ liquidação
    def settle_pending(self, now: float) -> int:
        """Liquida pelo crypto-price consolidado (abertura E fechamento lidos no fim, a abertura
        registrada na ordem só serve para acusar divergência). Fonte oficial (Gamma) entra na
        reconciliação assim que o mercado fecha."""
        n = 0
        for row in self.ledger.filled_unsettled():
            ts = int(row["ts"])
            if now < ts + WINDOW_S + 20:
                continue
            outcome = None
            close_price = None
            source = None
            ptb = self.pm.price_to_beat(ts)
            gamma = self.pm.gamma_outcome(ts)
            if gamma is not None:
                outcome, source = gamma, "gamma"
                if ptb and ptb.close_price is not None:
                    close_price = ptb.close_price
            elif ptb and ptb.completed and ptb.close_price is not None and ptb.open_price is not None:
                close_price = ptb.close_price
                outcome, source = outcome_from_prices(ptb.open_price, close_price), "local"
                if row.get("strike") is not None and abs(float(row["strike"]) - ptb.open_price) > 0.01:
                    self.ledger.journal("strike_mismatch_at_settle", ts=ts, order_strike=row["strike"], final_open=ptb.open_price)
            if outcome is None:
                continue
            filled = float(row.get("filled_shares") or 0)
            cost = float(row.get("cost_usd") or 0)
            won = row.get("side") == outcome
            pnl = round((filled if won else 0.0) - cost, 4)
            self.ledger.upsert(
                ts, status="settled", outcome=outcome, close_price=close_price, pnl_usd=pnl,
                outcome_source=source, reconciled=1 if source == "gamma" else 0,
            )
            self.ledger.journal("settled", ts=ts, side=row.get("side"), outcome=outcome, source=source, strike=row.get("strike"), close=close_price, filled=filled, cost=cost, pnl=pnl, p_model=row.get("p_model"))
            log.info("janela %s liquidada (%s): %s vs %s => pnl %.2f", ts, source, row.get("side"), outcome, pnl)
            if self.s.notify_trades:
                day_pnl = self.ledger.realized_pnl_usd(day_of(now))
                self.notifier.send(
                    f"settled:{ts}",
                    f"{'✅' if won else '❌'} {_hm(ts)} {row.get('side')} {filled:.2f} @ {float(row.get('fill_price') or 0):.2f} → {outcome} | "
                    f"PnL US$ {pnl:+.2f} | dia {day_pnl:+.2f} | total {self.ledger.realized_pnl_usd():+.2f}", 0)
            n += 1
        self.reconcile_settled(now)
        return n

    def _resolution(self, ts: int, now: float) -> Optional[str]:
        """Resultado pelo dinheiro da carteira (resgate recebido ou posição zerada). O endpoint de preço
        erra empates quase perfeitos, sempre para o lado de quem apostou; o CTF não erra."""
        if self.resolutions_fn is None:
            return None
        if now - self._resolutions_at >= 240:
            self._resolutions_at = now
            got = self.resolutions_fn(now - 36 * 3600)
            if got is not None:
                self._resolutions = got
        hit = self._resolutions.get(f"btc-updown-5m-{ts}")
        if hit is None:
            return None
        row = self.ledger.get(ts) or {}
        side = row.get("side")
        if hit[0] == "won":
            return side
        return "Up" if side == "Down" else "Down" if side == "Up" else None

    def reconcile_settled(self, now: float) -> int:
        """Confere liquidação local contra a verdade: primeiro o dinheiro da carteira, depois a Gamma
        (que deixou de devolver as janelas 5m). Corrige o PnL e alerta em cada divergência."""
        n = 0
        for row in self.ledger.settled_unreconciled():
            ts = int(row["ts"])
            if now < ts + WINDOW_S + 120:
                continue
            gamma = self._resolution(ts, now) or self.pm.gamma_outcome(ts)
            if gamma is None:
                if now > ts + WINDOW_S + 6 * 3600:
                    self.ledger.upsert(ts, reconciled=1)  # desiste após 6 h, mantém local
                continue
            if gamma != row.get("outcome"):
                filled = float(row.get("filled_shares") or 0)
                cost = float(row.get("cost_usd") or 0)
                pnl = round((filled if row.get("side") == gamma else 0.0) - cost, 4)
                self.ledger.upsert(ts, outcome=gamma, pnl_usd=pnl, outcome_source="gamma", reconciled=1)
                self.ledger.journal("settlement_mismatch", ts=ts, local=row.get("outcome"), gamma=gamma, old_pnl=row.get("pnl_usd"), new_pnl=pnl)
                log.error("janela %s: liquidação local %s divergiu da Gamma %s; corrigido pnl %.2f -> %.2f", ts, row.get("outcome"), gamma, row.get("pnl_usd") or 0, pnl)
                self.notifier.send(f"mismatch:{ts}", f"⚠️ Janela {_hm(ts)}: liquidação local {row.get('outcome')} divergiu da oficial {gamma}. PnL corrigido {row.get('pnl_usd') or 0:+.2f} → {pnl:+.2f}.", 0)
            else:
                self.ledger.upsert(ts, reconciled=1, outcome_source="gamma")
            n += 1
        return n

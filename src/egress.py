"""Vigia do egress do CLOB, com failover entre rotas (Tor era ponto único de falha).

Testa as rotas na ordem configurada e fica na primeira que responde de jurisdição onde a Polymarket
aceita abrir posição. Rota que cai, que não identifica o país ou que leva 403 de região fica de
quarentena e a próxima assume; sem nenhuma rota boa o motor para de postar (falha fechada, nunca
posta de jurisdição bloqueada). A troca de rota só é aplicada pelo motor, entre ordens."""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

log = logging.getLogger("egress")

# docs.polymarket.com/developers/CLOB/geoblock (lido em 18/09/2026). OFAC = bloqueio total;
# o resto é close-only na API: fecha posição, não abre. Para o motor, as duas listas = não opera.
OFAC_CC = {"IR", "SY", "CU", "KP"}
CLOSE_ONLY_CC = {
    "AU", "BY", "BE", "BI", "BR", "CF", "CD", "ET", "FR", "DE", "IQ", "IT", "LB", "LY", "MM", "NZ",
    "NI", "KP", "PL", "RU", "SG", "SO", "SK", "SS", "SD", "TW", "TH", "GB", "US", "UM", "VE", "YE", "ZW",
}
# Restrição por província/região: sem resolução de região confiável, o país inteiro fica de fora.
REGION_RESTRICTED_CC = {"CA", "UA"}
NO_OPEN_CC = OFAC_CC | CLOSE_ONLY_CC | REGION_RESTRICTED_CC


def redact(route: Optional[str]) -> str:
    """Rota sem credencial: socks5://user:senha@host:1080 vira socks5://***@host:1080."""
    if not route:
        return "DIRETO"
    return re.sub(r"//[^/@]*@", "//***@", route)


def probe(socks: Optional[str], tries: int = 3, get: Optional[Callable[..., Any]] = None) -> Optional[Tuple[str, str]]:
    """(ip, país) da saída, ou None se não resolver (quem chama trata como falha fechada)."""
    get = get or (lambda url, proxy: httpx.get(url, proxy=proxy, timeout=8).json())
    for _ in range(tries):
        try:
            r = get("https://ipwho.is/", socks)
            if r.get("success") and r.get("country_code"):
                return str(r.get("ip")), str(r["country_code"]).upper()
        except Exception:
            pass
    return None


@dataclass
class EgressState:
    ok: bool
    country: Optional[str]
    ip: Optional[str]
    reason: str
    checked_at: float


def classify(result: Optional[Tuple[str, str]], now: float) -> EgressState:
    if result is None:
        return EgressState(False, None, None, "saída não identificada (proxy caído?)", now)
    ip, cc = result
    if cc in NO_OPEN_CC:
        return EgressState(False, cc, ip, f"saída em {cc}, onde a Polymarket não aceita abrir posição", now)
    return EgressState(True, cc, ip, f"saída {cc}", now)


class EgressMonitor:
    """check() roda no boot; start() repete em thread a cada interval_s; mark_region_block() põe a rota
    atual de quarentena e procura outra. Estado velho conta como não-OK."""

    def __init__(
        self,
        socks: Any,
        interval_s: float = 300.0,
        notifier: Any = None,
        probe_fn: Callable[[Optional[str]], Optional[Tuple[str, str]]] = probe,
        clock: Callable[[], float] = time.time,
        region_hold_s: float = 300.0,
        fallbacks: Sequence[Optional[str]] = (),
    ):
        self.candidates: List[Optional[str]] = list(socks) if isinstance(socks, (list, tuple)) else [socks]
        for f in fallbacks:
            if f not in self.candidates:
                self.candidates.append(f)
        self.socks = self.candidates[0]      # rota em uso
        self.interval_s = interval_s
        self.notifier = notifier
        self._probe = probe_fn
        self._clock = clock
        self._state: Optional[EgressState] = None
        self._region_hold_s = region_hold_s
        self._held: Dict[Any, float] = {}    # rota -> instante em que sai da quarentena
        self._lock = threading.Lock()
        self._wake = threading.Event()

    @property
    def state(self) -> Optional[EgressState]:
        with self._lock:
            return self._state

    def ok(self) -> bool:
        st = self.state
        return bool(st and st.ok and self._clock() - st.checked_at <= 3 * self.interval_s)

    def reason(self) -> str:
        st = self.state
        if st is None:
            return "egress ainda não verificado"
        if st.ok and not self.ok():
            return "verificação de egress vencida"
        return st.reason

    def _ordered(self) -> List[Optional[str]]:
        """A rota em uso primeiro: só troca quando ela não serve."""
        cur = self.socks
        return [cur] + [c for c in self.candidates if c != cur]

    def check(self) -> EgressState:
        now = self._clock()
        new: Optional[EgressState] = None
        chosen = self.socks
        for cand in self._ordered():
            if now < self._held.get(cand, 0.0):
                # Rota de quarentena: o país do probe não garante a conexão do CLOB (podem sair por nós
                # diferentes), então depois de um 403 ela fica de fora pelo tempo mínimo.
                st = EgressState(False, None, None, "rota em quarentena após recusa de região (403)", now)
            else:
                st = classify(self._probe(cand), now)
            if new is None:
                new, chosen = st, cand
            if st.ok:
                new, chosen = st, cand
                break
        assert new is not None
        if chosen != self.socks and new.ok:
            old_route = self.socks
            self.socks = chosen
            self._journal_switch(old_route, chosen, new)
        with self._lock:
            old, self._state = self._state, new
        if self.notifier is not None and (old is None or old.ok != new.ok):
            if not new.ok:
                self.notifier.send("egress_down", f"⛔ Egress do CLOB fora: {new.reason}. Motor sem postar ordens até normalizar.")
            elif old is not None:
                self.notifier.send("egress_up", f"✅ Egress do CLOB normalizado ({new.reason}).")
        if old is None or old.ok != new.ok or old.country != new.country:
            log.warning("egress: ok=%s | %s", new.ok, new.reason)
        return new

    def _journal_switch(self, old_route: Optional[str], new_route: Optional[str], st: EgressState) -> None:
        log.warning("egress trocou de rota: %s -> %s (%s)", redact(old_route), redact(new_route), st.reason)
        if self.notifier is not None:
            self.notifier.send("egress_switch", f"🔀 Rota do CLOB trocada para {redact(new_route)} ({st.reason}).", 0)

    def desired_proxy(self) -> Optional[str]:
        """Rota que o motor deve estar usando agora (aplicada por ele, nunca por esta thread)."""
        return self.socks

    def mark_region_block(self) -> None:
        """O CLOB recusou por região: põe esta rota de quarentena, fecha já e procura outra."""
        now = self._clock()
        self._held[self.socks] = now + self._region_hold_s
        with self._lock:
            st = self._state
            self._state = EgressState(False, st.country if st else None, st.ip if st else None,
                                      "CLOB recusou por região (403)", now)
        if self.notifier is not None:
            self.notifier.send("egress_down", "⛔ CLOB recusou ordem por região (403). Motor sem postar ordens até a saída normalizar.")
        self._wake.set()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="egress", daemon=True).start()

    def _loop(self) -> None:
        while True:
            fired = self._wake.wait(self.interval_s)
            self._wake.clear()
            if fired:
                time.sleep(5)  # deixa o motor terminar a janela antes de procurar outra rota
            try:
                self.check()
            except Exception:
                log.exception("falha na verificação de egress")

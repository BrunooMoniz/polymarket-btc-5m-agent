"""Shadow: motores em PAPER rodando ao lado do live, no mesmo processo, com o mesmo feed Chainlink
e os mesmos vereditos do Jev, cada um com seus parâmetros e seu ledger. Nada aqui toca o broker
live; uma exceção no shadow morre na thread dele."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger("shadow")


class SharedJevGate:
    """Um veredito por janela vale para todos os motores por share_s: o estado enviado ao Jev vem do
    mesmo feed e do mesmo strike, então a segunda chamada seria paga para ouvir a mesma resposta."""

    def __init__(self, gate: Any, share_s: float = 8.0, clock: Callable[[], float] = time.time):
        self._gate = gate
        self._share_s = share_s
        self._clock = clock
        self._lock = threading.Lock()
        self._hit: Optional[Tuple[Any, float, Any]] = None

    def evaluate(self, state: Dict[str, Any]) -> Any:
        key = (state.get("market") or {}).get("window_start_utc")
        with self._lock:  # quem chega durante a chamada espera e reaproveita
            if self._hit is not None and self._hit[0] == key and self._clock() - self._hit[1] < self._share_s:
                if isinstance(self._hit[2], BaseException):
                    # Jev fora: os outros motores não repetem o timeout em fila. Exceção NOVA a cada
                    # relançamento; relançar a mesma instância vai empilhando frames no __traceback__.
                    err = self._hit[2]
                    raise RuntimeError(f"Jev indisponível (falha compartilhada): {err!r}") from None
                return self._hit[2]
            try:
                verdict = self._gate.evaluate(state)
            except Exception as e:
                self._hit = (key, self._clock(), e)
                raise
            self._hit = (key, self._clock(), verdict)
            return verdict


class CachedPM:
    """Book com validade curta, compartilhado entre os shadows: N motores não multiplicam as consultas
    ao CLOB. O live não usa isto (sempre book fresco)."""

    def __init__(self, pm: Any, book_ttl_s: float = 1.5, clock: Callable[[], float] = time.time):
        self._pm = pm
        self._ttl = book_ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._books: Dict[str, Tuple[float, Any]] = {}

    def book(self, token_id: str) -> Any:
        now = self._clock()
        with self._lock:
            hit = self._books.get(token_id)
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1]
        book = self._pm.book(token_id)
        with self._lock:
            if len(self._books) > 64:
                self._books.clear()
            self._books[token_id] = (now, book)
        return book

    def __getattr__(self, name: str) -> Any:  # market, price_to_beat, gamma_outcome: direto
        return getattr(self._pm, name)


def start_shadow(name: str, engine: Any) -> threading.Thread:
    def run() -> None:
        try:
            engine.run_forever()
        except BaseException:
            log.exception("shadow %s morreu; o live segue", name)

    t = threading.Thread(target=run, name=f"shadow-{name}", daemon=True)
    t.start()
    return t

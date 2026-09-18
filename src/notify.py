"""Alertas no Telegram. Fila em thread própria: um envio lento ou falho nunca atrasa o loop de
2 s nem o TTL da ordem, e nunca levanta para quem chama. Vai direto (sem o proxy do CLOB)."""
from __future__ import annotations

import html
import logging
import queue
import re
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional

import httpx

log = logging.getLogger("notify")

API = "https://api.telegram.org/bot{token}/sendMessage"


class NullNotifier:
    enabled = False

    def send(self, key: str, text: str, min_interval_s: Optional[float] = None, mono: Optional[str] = None) -> bool:
        return False


class Notifier:
    """send(key, texto): no máximo um envio por `key` a cada min_interval_s (anti-rajada)."""

    enabled = True

    def __init__(
        self,
        token: str,
        chat_id: str,
        prefix: str = "",
        min_interval_s: float = 600.0,
        post: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.time,
        start: bool = True,
    ):
        self._url = API.format(token=token)
        self._chat_id = chat_id
        self._prefix = prefix
        self._min_interval_s = min_interval_s
        self._post = post or (lambda url, payload: httpx.post(url, json=payload, timeout=8.0))
        self._clock = clock
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=50)  # corpo já em HTML escapado
        if start:
            threading.Thread(target=self._worker, name="notify", daemon=True).start()

    def send(self, key: str, text: str, min_interval_s: Optional[float] = None, mono: Optional[str] = None) -> bool:
        """`mono` vai num bloco monoespaçado (tabelas do relatório), cortado para caber no limite do Telegram."""
        gap = self._min_interval_s if min_interval_s is None else min_interval_s
        now = self._clock()
        with self._lock:
            if now - self._last.get(key, -1e18) < gap:
                return False
            self._last[key] = now
        try:
            body = re.sub(r"&[^;]{0,6}$", "", html.escape(f"{self._prefix}{text}")[:1500])
            if mono:
                room = 4000 - len(body) - len("\n<pre></pre>")  # limite do Telegram: 4096
                esc = re.sub(r"&[^;]{0,6}$", "", html.escape(mono)[:room])  # corte nunca parte uma entidade
                body += "\n<pre>" + esc + "</pre>"
            self._q.put_nowait(body)
        except queue.Full:
            return False
        return True

    def drain_once(self) -> bool:
        """Envia uma mensagem da fila (o worker chama em laço; os testes chamam direto)."""
        try:
            text = self._q.get_nowait()
        except queue.Empty:
            return False
        try:
            r = self._post(self._url, {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
            code = getattr(r, "status_code", 200)
            if code >= 400:
                log.warning("telegram devolveu %s", code)
        except Exception as e:  # nunca propaga: alerta é melhor esforço
            log.warning("telegram falhou: %s", type(e).__name__)
        return True

    def _worker(self) -> None:
        while True:
            if not self.drain_once():
                time.sleep(0.5)


def from_env(env: Mapping[str, str], prefix: str = "") -> Any:
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes: alertas desligados")
        return NullNotifier()
    return Notifier(token, chat, prefix=prefix)

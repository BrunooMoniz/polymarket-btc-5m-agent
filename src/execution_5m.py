"""Brokers. PaperBroker simula fill maker (só quando o ask do book chega ao nosso limite).
LiveBroker usa py_clob_client_v2 com post_only=True (nunca vira taker, taxa zero + rebate).
Imports do cliente ficam dentro do LiveBroker para o resto rodar sem ele."""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from src.polymarket_5m import Book

log = logging.getLogger("execution_5m")

# Status terminais do CLOB. Qualquer outro (LIVE, OPEN, DELAYED, desconhecido) é tratado como
# ABERTO: o motor então cancela explicitamente em vez de supor que a ordem morreu.
TERMINAL_STATUSES = {"MATCHED", "FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "INVALID"}


@dataclass
class OrderState:
    order_id: str
    token_id: str
    price: float
    size: float
    filled: float = 0.0
    avg_price: float = 0.0
    open: bool = True
    status: str = ""


def order_state_from_clob(order_id: str, o: Dict[str, Any]) -> OrderState:
    size = float(o.get("original_size") or o.get("size") or 0)
    filled = float(o.get("size_matched") or 0)
    price = float(o.get("price") or 0)
    status = str(o.get("status") or "").upper()
    open_ = status not in TERMINAL_STATUSES and (size <= 0 or filled < size)
    return OrderState(
        order_id=order_id, token_id=str(o.get("asset_id") or ""), price=price, size=size,
        filled=filled, avg_price=price, open=open_, status=status,
    )


def order_error_kind(exc: BaseException) -> str:
    """'cross' = post-only cruzaria o book; 'region' = 403 de jurisdição; 'rejected' = o CLOB respondeu
    4xx (a ordem não existe); 'unknown' = sem resposta do CLOB (rede, timeout, 5xx): a ordem PODE ter
    sido aceita, porque o cliente transforma qualquer falha de rede em exceção sem status."""
    text = repr(exc).lower()
    if "crosses book" in text:
        return "cross"
    if "restricted in your region" in text or ("403" in text and "region" in text):
        return "region"
    if re.search(r"status_code=4\d\d", text):
        return "rejected"
    return "unknown"


def fills_from_trades(trades: Any, order_id: str) -> "tuple[float, float]":
    """(quantidade, preço médio) executados da nossa ordem, somando os trades em que ela aparece.
    O CLOB esquece a ordem em get_order pouco depois de ela sair do book; os trades ficam."""
    qty = notional = 0.0
    for t in trades or []:
        if str(t.get("status") or "").upper() == "FAILED":
            continue
        if str(t.get("taker_order_id") or "") == order_id:
            q, px = float(t.get("size") or 0), float(t.get("price") or 0)
            qty, notional = qty + q, notional + q * px
        for m in t.get("maker_orders") or []:
            if str(m.get("order_id") or "") == order_id:
                q, px = float(m.get("matched_amount") or 0), float(m.get("price") or 0)
                qty, notional = qty + q, notional + q * px
    return round(qty, 6), (notional / qty if qty > 0 else 0.0)


class PaperBroker:
    """Fill maker conservador: a ordem só executa se o melhor ask cair até o nosso preço."""

    def __init__(self, book_fn: Callable[[str], Optional[Book]], bankroll_usd: float):
        self._book_fn = book_fn
        self.bankroll_usd = bankroll_usd
        self.orders: Dict[str, OrderState] = {}
        self.mode = "paper"

    def place_maker(self, token_id: str, price: float, size: float) -> str:
        oid = f"paper-{uuid.uuid4().hex[:10]}"
        self.orders[oid] = OrderState(order_id=oid, token_id=token_id, price=price, size=size, status="LIVE")
        return oid

    def poll(self, order_id: str) -> OrderState:
        st = self.orders.get(order_id)
        if st is None:  # ordem de um processo anterior: no paper ela morreu junto com ele
            return OrderState(order_id=order_id, token_id="", price=0.0, size=0.0, open=False, status="CANCELED")
        if not st.open:
            return st
        book = self._book_fn(st.token_id)
        if book is not None and book.best_ask is not None and book.best_ask <= st.price + 1e-9:
            st.filled = st.size
            st.avg_price = st.price
            st.open = False
            st.status = "MATCHED"
        return st

    def place_taker(self, token_id: str, price: float, size: float) -> OrderState:
        """Come o ask: só executa se o ask ainda estiver no nosso preço (FOK), senão morre."""
        book = self._book_fn(token_id)
        oid = f"paper-tk-{uuid.uuid4().hex[:8]}"
        st = OrderState(order_id=oid, token_id=token_id, price=price, size=size, status="FOK")
        if book is not None and book.best_ask is not None and book.best_ask <= price + 1e-9:
            st.filled, st.avg_price, st.status = size, book.best_ask, "MATCHED"
        else:
            st.status = "KILLED"
        st.open = False
        self.orders[oid] = st
        return st

    def sell_taker(self, token_id: str, price: float, size: float) -> OrderState:
        """Vende no bid (FOK). Devolve o executado; 0 se o bid saiu do preço."""
        book = self._book_fn(token_id)
        oid = f"paper-sell-{uuid.uuid4().hex[:8]}"
        st = OrderState(order_id=oid, token_id=token_id, price=price, size=size, status="FOK")
        if book is not None and book.best_bid is not None and book.best_bid >= price - 1e-9:
            st.filled, st.avg_price, st.status = size, book.best_bid, "MATCHED"
        else:
            st.status = "KILLED"
        st.open = False
        self.orders[oid] = st
        return st

    def set_proxy(self, socks: Optional[str]) -> bool:
        return False

    def resolve(self, order_id: str, since_ts: float) -> OrderState:
        return self.poll(order_id)

    def cancel(self, order_id: str) -> bool:
        st = self.orders.get(order_id)
        if st and st.open:
            st.open = False
            st.status = "CANCELED"
        return True

    def cancel_all(self) -> None:
        for st in self.orders.values():
            if st.open:
                st.open = False
                st.status = "CANCELED"

    def collateral(self, open_cost_usd: float = 0.0, realized_pnl_usd: float = 0.0) -> float:
        return self.bankroll_usd + realized_pnl_usd - open_cost_usd


class LiveBroker:
    """Cliente do CLOB construído sob demanda: derivar a credencial exige rede, e no arranque a rota
    pode estar fora. Estourar ali punha o serviço em laço de reinício (medido em 18/09/2026, com a
    saída Tor na Alemanha); agora o motor sobe fechado e o cliente nasce na primeira ordem."""

    def __init__(self, private_key: str, proxy_wallet: Optional[str], chain_id: int = 137, client: Any = None,
                 socks_proxy: Optional[str] = None):
        self.mode = "live"
        self.proxy_wallet = proxy_wallet
        self.socks_proxy = socks_proxy
        self._retired: Any = None
        self._sig_type = 3 if proxy_wallet else 0
        self._private_key = private_key
        self._chain_id = chain_id
        self._client = client          # injeção nos testes: pronto, sem rede
        self._proxy_applied = client is not None
        if client is None and not private_key:
            raise ValueError("POLYMARKET_PRIVATE_KEY ausente: live exige chave explícita")

    @property
    def client(self) -> Any:
        if self._client is None:
            self._apply_proxy(self.socks_proxy)
            from py_clob_client_v2.client import ClobClient

            c = ClobClient("https://clob.polymarket.com", key=self._private_key, chain_id=self._chain_id,
                           funder=self.proxy_wallet, signature_type=self._sig_type)
            c.set_api_creds(c.create_or_derive_api_key())   # só aqui a rede é obrigatória
            self._client = c
            log.info("cliente do CLOB pronto (rota %s)", self.socks_proxy or "DIRETO")
        return self._client

    def _apply_proxy(self, socks: Optional[str]) -> None:
        import httpx

        import py_clob_client_v2.http_helpers.helpers as _hh

        old = getattr(_hh, "_http_client", None)
        _hh._http_client = httpx.Client(http2=True, proxy=socks, timeout=12.0) if socks else httpx.Client(http2=True, timeout=12.0)
        # Fecha só o penúltimo: fechar o recém-substituído abortaria requisição ainda em voo nele.
        try:
            if self._retired is not None:
                self._retired.close()
        except Exception:
            pass
        self._retired = old
        self._proxy_applied = True

    def set_proxy(self, socks: Optional[str]) -> bool:
        """Troca a rota do CLOB. Chamada só pelo motor, entre ordens, nunca com chamada em voo: o
        cliente da lib guarda UM httpx.Client de módulo e trocá-lo no meio de uma requisição a mataria."""
        if socks == self.socks_proxy and self._proxy_applied:
            return False
        self.socks_proxy = socks
        if self._client is None:
            return False          # o cliente nasce já na rota nova; nada a trocar ainda
        self._apply_proxy(socks)
        from src.egress import redact

        log.warning("rota do CLOB trocada para %s", redact(socks))
        return True

    def place_taker(self, token_id: str, price: float, size: float) -> OrderState:
        """FOK: executa inteiro no ato ou morre. Nunca deixa ordem descansando no book."""
        from py_clob_client_v2.clob_types import OrderArgs, OrderType

        resp = self.client.create_and_post_order(
            OrderArgs(token_id=token_id, price=price, size=size, side="BUY"), order_type=OrderType.FOK
        ) or {}
        return self._from_immediate(resp, token_id, price, size)

    def sell_taker(self, token_id: str, price: float, size: float) -> OrderState:
        from py_clob_client_v2.clob_types import OrderArgs, OrderType

        resp = self.client.create_and_post_order(
            OrderArgs(token_id=token_id, price=price, size=size, side="SELL"), order_type=OrderType.FOK
        ) or {}
        return self._from_immediate(resp, token_id, price, size)

    def _from_immediate(self, resp: Dict[str, Any], token_id: str, price: float, size: float) -> OrderState:
        """Resposta de ordem FOK. Se o CLOB não disser o quanto casou, os trades dizem depois."""
        oid = str(resp.get("orderID") or resp.get("id") or "")
        matched = resp.get("size_matched") if resp.get("size_matched") is not None else resp.get("takingAmount")
        status = str(resp.get("status") or "").upper()
        if matched is None and oid:
            try:
                return self.resolve(oid, time.time() - 60)
            except Exception as e:
                log.warning("não consegui confirmar a FOK %s: %s", oid[:12], e)
                return OrderState(order_id=oid, token_id=token_id, price=price, size=size, open=True, status=status or "UNKNOWN")
        filled = float(matched or 0)
        return OrderState(order_id=oid, token_id=token_id, price=price, size=size, filled=filled,
                          avg_price=price, open=False, status=status or ("MATCHED" if filled > 0 else "KILLED"))

    def place_maker(self, token_id: str, price: float, size: float) -> str:
        from py_clob_client_v2.clob_types import OrderArgs, OrderType

        args = OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
        # post_only: se cruzaria o spread o CLOB rejeita em vez de executar como taker.
        resp = self.client.create_and_post_order(args, order_type=OrderType.GTC, post_only=True)
        oid = (resp or {}).get("orderID") or (resp or {}).get("id")
        if not oid:
            raise RuntimeError(f"CLOB não devolveu orderID: {resp}")
        return str(oid)

    def poll(self, order_id: str) -> OrderState:
        return order_state_from_clob(order_id, self.client.get_order(order_id) or {})

    def resolve(self, order_id: str, since_ts: float) -> OrderState:
        """Destino definitivo de uma ordem antiga. get_order devolve None para ordem que já saiu do
        book (executada, cancelada ou inexistente; medido em 18/09/2026), e aí quem sabe são os trades."""
        from py_clob_client_v2.clob_types import TradeParams

        o = self.client.get_order(order_id)
        if o:
            return order_state_from_clob(order_id, o)
        trades = self.client.get_trades(TradeParams(after=int(since_ts) - 5, before=int(since_ts) + 900))
        qty, avg = fills_from_trades(trades, order_id)
        return OrderState(order_id=order_id, token_id="", price=avg, size=qty, filled=qty, avg_price=avg, open=False, status="GONE")

    def cancel(self, order_id: str) -> bool:
        """True só quando o CLOB confirmou o cancelamento (ou a ordem já era terminal)."""
        try:
            from py_clob_client_v2.clob_types import OrderPayload
        except ImportError:  # testes sem o cliente: o CLOB só lê payload.orderID
            from types import SimpleNamespace as OrderPayload  # type: ignore

        try:
            resp = self.client.cancel_order(OrderPayload(orderID=order_id)) or {}
        except Exception as e:
            log.warning("cancel %s falhou: %s", order_id, e)
            return False
        canceled = [str(x) for x in (resp.get("canceled") or [])]
        not_canceled = resp.get("not_canceled") or {}
        if order_id in canceled:
            return True
        if order_id in not_canceled:
            log.warning("cancel %s recusado: %s", order_id, not_canceled.get(order_id))
        return not self.poll(order_id).open

    def cancel_all(self) -> None:
        self.client.cancel_all()

    def collateral(self, open_cost_usd: float = 0.0, realized_pnl_usd: float = 0.0) -> float:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        res = self.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=self._sig_type)
        )
        return int(res.get("balance", 0)) / 1e6

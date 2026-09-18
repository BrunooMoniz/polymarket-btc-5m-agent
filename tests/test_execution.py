from src.execution_5m import LiveBroker, order_state_from_clob


def test_order_state_terminal_and_unknown_statuses():
    live = order_state_from_clob("o1", {"status": "LIVE", "original_size": "5", "size_matched": "0", "price": "0.48"})
    assert live.open and live.filled == 0 and live.size == 5
    part = order_state_from_clob("o1", {"status": "LIVE", "original_size": "5", "size_matched": "2", "price": "0.48"})
    assert part.open and part.filled == 2
    done = order_state_from_clob("o1", {"status": "MATCHED", "original_size": "5", "size_matched": "5"})
    assert not done.open and done.filled == 5
    canc = order_state_from_clob("o1", {"status": "CANCELED", "original_size": "5", "size_matched": "1"})
    assert not canc.open and canc.filled == 1
    weird = order_state_from_clob("o1", {"status": "DELAYED", "original_size": "5", "size_matched": "0"})
    assert weird.open  # desconhecido = aberto: o motor cancela explicitamente
    empty = order_state_from_clob("o1", {})
    assert empty.open


class FakeClob:
    def __init__(self):
        self.cancel_resp = {"canceled": ["o1"], "not_canceled": {}}
        self.order = {"status": "LIVE", "original_size": "5", "size_matched": "0", "price": "0.48"}
        self.raise_on_cancel = False
        self.calls = []

    def cancel_order(self, payload):
        self.calls.append(("cancel", payload.orderID))
        if self.raise_on_cancel:
            raise RuntimeError("boom")
        return self.cancel_resp

    def get_order(self, oid):
        self.calls.append(("get", oid))
        return self.order


def test_live_cancel_confirmation_paths():
    c = FakeClob()
    b = LiveBroker("", "0xproxy", client=c)
    assert b.cancel("o1") is True
    assert c.calls[-1] == ("cancel", "o1")

    c.cancel_resp = {"canceled": [], "not_canceled": {"o1": "already filled"}}
    c.order = {"status": "MATCHED", "original_size": "5", "size_matched": "5"}
    assert b.cancel("o1") is True   # recusado porque já terminal: poll confirma fechado
    assert b.poll("o1").filled == 5

    c.cancel_resp = {"canceled": [], "not_canceled": {"o1": "unknown"}}
    c.order = {"status": "LIVE", "original_size": "5", "size_matched": "0"}
    assert b.cancel("o1") is False  # ainda aberta e não cancelada

    c.raise_on_cancel = True
    assert b.cancel("o1") is False

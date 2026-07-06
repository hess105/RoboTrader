"""Alpaca implementation of ExecutionClient via alpaca-py's TradingClient.

paper=True/False is the ONLY difference between modes; it is derived from
Settings.mode, never passed ad hoc. Every submit carries the intent's
client_order_id — Alpaca rejects duplicates server-side, which is what makes
OrderManager's retry protocol safe.

NOTE: exercised against the paper API only once keys exist (make keys-paper);
not unit-tested beyond import. Fractional (notional) orders require DAY tif.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from core.models import Mode, Order, OrderIntent, OrderStatus, Position, Side
from core.settings import BrokerCreds

_STATUS = {
    "new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
}


class AlpacaExecution:
    def __init__(self, creds: BrokerCreds, mode: Mode):
        if mode is Mode.BACKTEST:
            raise ValueError("Backtest uses backtest.engine, not a broker client.")
        from alpaca.trading.client import TradingClient

        self.paper = mode is Mode.PAPER
        self._client = TradingClient(
            creds.key_id.get_secret_value(),
            creds.secret_key.get_secret_value(),
            paper=self.paper,
        )

    def submit(self, intent: OrderIntent) -> Order:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        common = dict(
            symbol=intent.signal.symbol,
            side=OrderSide.BUY if intent.signal.side is Side.BUY else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=intent.client_order_id,
            notional=float(intent.notional) if intent.notional is not None else None,
            qty=float(intent.qty) if intent.qty is not None else None,
        )
        if intent.limit_price is not None:
            req = LimitOrderRequest(limit_price=float(intent.limit_price), **common)
        else:
            req = MarketOrderRequest(**common)
        return self._to_order(self._client.submit_order(req))

    def cancel(self, client_order_id: str) -> None:
        order = self.get_order_by_client_id(client_order_id)
        if order is not None and order.broker_order_id:
            self._client.cancel_order_by_id(order.broker_order_id)

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        from alpaca.common.exceptions import APIError

        try:
            return self._to_order(self._client.get_order_by_client_id(client_order_id))
        except APIError:
            return None

    def open_orders(self) -> list[Order]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        raw = self._client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        return [self._to_order(o) for o in raw]

    def positions(self) -> list[Position]:
        out = []
        for p in self._client.get_all_positions():
            out.append(Position(
                symbol=p.symbol,
                qty=Decimal(str(p.qty)),
                avg_entry=Decimal(str(p.avg_entry_price)),
                stop_price=None,                       # enriched from journal
                opened_at=datetime.now(timezone.utc),  # broker doesn't expose it
                bucket="",
            ))
        return out

    def positions_detail(self) -> list[dict]:
        """GUI-friendly position rows with broker-computed marks and P&L."""
        out = []
        for p in self._client.get_all_positions():
            out.append({
                "symbol": p.symbol, "qty": float(p.qty),
                "avg_entry": float(p.avg_entry_price),
                "current_price": float(p.current_price or 0),
                "market_value": float(p.market_value or 0),
                "unrealized_pl": float(p.unrealized_pl or 0),
                "unrealized_plpc": float(p.unrealized_plpc or 0),
            })
        return out

    def market_is_open(self) -> bool:
        return bool(self._client.get_clock().is_open)

    def account_equity(self) -> tuple:
        a = self._client.get_account()
        return Decimal(str(a.equity)), Decimal(str(a.cash)), Decimal(str(a.buying_power))

    def close_all_positions(self) -> list[Order]:
        self._client.close_all_positions(cancel_orders=True)
        return self.open_orders()

    @staticmethod
    def _to_order(o) -> Order:
        status = getattr(o.status, "value", str(o.status))
        return Order(
            client_order_id=o.client_order_id,
            broker_order_id=str(o.id),
            symbol=o.symbol,
            side=Side(getattr(o.side, "value", str(o.side))),
            status=_STATUS.get(status, OrderStatus.SUBMITTED),
            notional=Decimal(str(o.notional)) if o.notional is not None else None,
            qty=Decimal(str(o.qty)) if o.qty is not None else None,
            filled_qty=Decimal(str(o.filled_qty or 0)),
            avg_fill_price=Decimal(str(o.filled_avg_price)) if o.filled_avg_price else None,
            submitted_at=o.submitted_at,
        )

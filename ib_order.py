"""
IB Order Manager
================
Place, modify, and cancel orders on Interactive Brokers via ib_insync.

Requirements:
  pip install ib_insync

IB Gateway / TWS must be running and API connections must be enabled.
Default connection: localhost:7497 (TWS paper) or localhost:4002 (IB Gateway paper).
Live ports: TWS=7496, IB Gateway=4001.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Optional

# ib_insync / eventkit call get_event_loop() at import time; provide a loop
# so the import succeeds on Python 3.10+ where none is created automatically.
asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import (
    IB,
    Contract,
    Stock,
    Option,
    Future,
    Forex,
    CFD,
    Order,
    MarketOrder,
    LimitOrder,
    StopOrder,
    StopLimitOrder,
    BracketOrder,
    util,
)


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

async def connect(host: str = "127.0.0.1", port: int = 7497, client_id: int = 1) -> IB:
    """Connect to TWS / IB Gateway and return the IB instance."""
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id)
    print(f"Connected to IB at {host}:{port}  (clientId={client_id})")
    return ib


def disconnect(ib: IB) -> None:
    ib.disconnect()
    print("Disconnected from IB.")


# ---------------------------------------------------------------------------
# Contract builders
# ---------------------------------------------------------------------------

def make_stock(symbol: str, exchange: str = "SMART", currency: str = "USD") -> Stock:
    return Stock(symbol, exchange, currency)


def make_option(
    symbol: str,
    last_trade_date: str,  # e.g. "20251219"
    strike: float,
    right: str,            # "C" or "P"
    exchange: str = "SMART",
    currency: str = "USD",
) -> Option:
    return Option(symbol, last_trade_date, strike, right, exchange, currency=currency)


def make_future(
    symbol: str,
    last_trade_date: str,  # e.g. "20251219"
    exchange: str = "CME",
    currency: str = "USD",
) -> Future:
    return Future(symbol, last_trade_date, exchange, currency=currency)


def make_forex(pair: str) -> Forex:
    """pair e.g. 'EURUSD'"""
    return Forex(pair)


def make_cfd(symbol: str, exchange: str = "SMART", currency: str = "USD") -> CFD:
    return CFD(symbol, exchange, currency)


# ---------------------------------------------------------------------------
# Order builders
# ---------------------------------------------------------------------------

def market_order(action: str, quantity: float) -> MarketOrder:
    """action: 'BUY' or 'SELL'"""
    return MarketOrder(action, quantity)


def limit_order(action: str, quantity: float, limit_price: float) -> LimitOrder:
    o = LimitOrder(action, quantity, limit_price)
    o.tif = "DAY"
    return o


def stop_order(action: str, quantity: float, stop_price: float) -> StopOrder:
    o = StopOrder(action, quantity, stop_price)
    o.tif = "DAY"
    return o


def stop_limit_order(
    action: str, quantity: float, limit_price: float, stop_price: float
) -> StopLimitOrder:
    o = StopLimitOrder(action, quantity, limit_price, stop_price)
    o.tif = "DAY"
    return o


def bracket_order(
    action: str,
    quantity: float,
    entry_price: float,
    take_profit_price: float,
    stop_loss_price: float,
    ib: IB,
) -> BracketOrder:
    """
    Returns a BracketOrder (parent limit + take-profit limit + stop-loss stop).
    All three orders must be placed together.
    """
    return ib.bracketOrder(
        action,
        quantity,
        entry_price,
        take_profit_price,
        stop_loss_price,
    )


# ---------------------------------------------------------------------------
# Order management
# ---------------------------------------------------------------------------

async def place(ib: IB, contract: Contract, order: Order):
    """Place an order and wait for acknowledgment."""
    trade = ib.placeOrder(contract, order)
    await asyncio.sleep(1)  # allow time for the order to register
    print(f"Placed order: {trade.order.action} {trade.order.totalQuantity} "
          f"{contract.symbol}  orderId={trade.order.orderId}  "
          f"status={trade.orderStatus.status}")
    return trade


async def place_bracket(ib: IB, contract: Contract, bracket: BracketOrder):
    """Place all legs of a bracket order."""
    trades = []
    for order in bracket:
        trade = ib.placeOrder(contract, order)
        trades.append(trade)
    await asyncio.sleep(1)
    for trade in trades:
        print(f"  Bracket leg: {trade.order.action} {trade.order.totalQuantity} "
              f"orderId={trade.order.orderId}  status={trade.orderStatus.status}")
    return trades


async def cancel(ib: IB, trade) -> None:
    ib.cancelOrder(trade.order)
    await asyncio.sleep(1)
    print(f"Cancelled orderId={trade.order.orderId}  status={trade.orderStatus.status}")


async def modify(ib: IB, trade, **kwargs) -> None:
    """
    Modify an open order.  Pass keyword args for fields to change, e.g.:
        modify(ib, trade, lmtPrice=150.0, totalQuantity=20)
    """
    order = trade.order
    for key, value in kwargs.items():
        setattr(order, key, value)
    ib.placeOrder(trade.contract, order)
    await asyncio.sleep(1)
    print(f"Modified orderId={order.orderId}  {kwargs}")


# ---------------------------------------------------------------------------
# Account / position helpers
# ---------------------------------------------------------------------------

async def print_positions(ib: IB) -> None:
    positions = await ib.positionsAsync()
    if not positions:
        print("No open positions.")
        return
    print(f"{'Symbol':<10} {'SecType':<8} {'Qty':>10} {'AvgCost':>12}")
    print("-" * 44)
    for pos in positions:
        print(f"{pos.contract.symbol:<10} {pos.contract.secType:<8} "
              f"{pos.position:>10.2f} {pos.avgCost:>12.4f}")


async def print_open_orders(ib: IB) -> None:
    orders = await ib.openOrdersAsync()
    if not orders:
        print("No open orders.")
        return
    print(f"{'OrderId':<10} {'Symbol':<10} {'Action':<6} {'Qty':>8} {'Type':<12} {'LmtPx':>10} {'StpPx':>10}")
    print("-" * 72)
    for o in orders:
        print(f"{o.orderId:<10} {'':<10} {o.action:<6} {o.totalQuantity:>8.2f} "
              f"{o.orderType:<12} {getattr(o,'lmtPrice',0):>10.4f} "
              f"{getattr(o,'auxPrice',0):>10.4f}")


async def print_account_summary(ib: IB) -> None:
    summary = await ib.accountSummaryAsync()
    keys = {"NetLiquidation", "TotalCashValue", "UnrealizedPnL", "RealizedPnL",
            "BuyingPower", "GrossPositionValue"}
    print(f"{'Tag':<25} {'Value':>18} {'Currency':<6}")
    print("-" * 52)
    for item in summary:
        if item.tag in keys:
            print(f"{item.tag:<25} {item.value:>18} {item.currency:<6}")


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Interactive Brokers order manager demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Market buy 10 shares of AAPL
  python ib_order.py --action BUY --qty 10 --symbol AAPL --order-type market

  # Limit sell 5 MSFT at $420
  python ib_order.py --action SELL --qty 5 --symbol MSFT --order-type limit --limit-price 420

  # Bracket order: buy 1 TSLA at market, TP=300, SL=240
  python ib_order.py --action BUY --qty 1 --symbol TSLA --order-type bracket \\
      --entry-price 270 --take-profit 300 --stop-loss 240

  # Show positions and open orders only (no trade)
  python ib_order.py --info-only

  # Connect to IB Gateway paper on port 4002
  python ib_order.py --port 4002 --info-only
""",
    )
    p.add_argument("--host", default="127.0.0.1", help="TWS/Gateway host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=7497, help="TWS/Gateway port (default: 7497 = TWS paper)")
    p.add_argument("--client-id", type=int, default=1, help="Client ID (default: 1)")

    p.add_argument("--symbol", help="Ticker symbol, e.g. AAPL")
    p.add_argument("--sec-type", default="STK", choices=["STK", "OPT", "FUT", "CASH", "CFD"],
                   help="Security type (default: STK)")
    p.add_argument("--exchange", default="SMART", help="Exchange (default: SMART)")
    p.add_argument("--currency", default="USD", help="Currency (default: USD)")

    # Option / Future specific
    p.add_argument("--expiry", help="Expiry date YYYYMMDD (for OPT / FUT)")
    p.add_argument("--strike", type=float, help="Strike price (for OPT)")
    p.add_argument("--right", choices=["C", "P"], help="Call or Put (for OPT)")

    p.add_argument("--action", choices=["BUY", "SELL"], help="Order action")
    p.add_argument("--qty", type=float, help="Order quantity")
    p.add_argument("--order-type", choices=["market", "limit", "stop", "stop_limit", "bracket"],
                   default="market", help="Order type (default: market)")
    p.add_argument("--limit-price", type=float, help="Limit price")
    p.add_argument("--stop-price", type=float, help="Stop price")
    p.add_argument("--entry-price", type=float, help="Entry limit price (bracket)")
    p.add_argument("--take-profit", type=float, help="Take-profit price (bracket)")
    p.add_argument("--stop-loss", type=float, help="Stop-loss price (bracket)")

    p.add_argument("--info-only", action="store_true",
                   help="Print account summary, positions, and open orders then exit")
    return p.parse_args()


def build_contract(args: argparse.Namespace) -> Contract:
    st = args.sec_type
    if st == "STK":
        return make_stock(args.symbol, args.exchange, args.currency)
    elif st == "OPT":
        for required in ("expiry", "strike", "right"):
            if not getattr(args, required):
                sys.exit(f"--{required} is required for OPT orders")
        return make_option(args.symbol, args.expiry, args.strike, args.right,
                           args.exchange, args.currency)
    elif st == "FUT":
        if not args.expiry:
            sys.exit("--expiry is required for FUT orders")
        return make_future(args.symbol, args.expiry, args.exchange, args.currency)
    elif st == "CASH":
        return make_forex(args.symbol)
    elif st == "CFD":
        return make_cfd(args.symbol, args.exchange, args.currency)
    else:
        sys.exit(f"Unsupported security type: {st}")


def build_order(args: argparse.Namespace, ib: IB) -> Order | BracketOrder:
    ot = args.order_type
    if ot == "market":
        return market_order(args.action, args.qty)
    elif ot == "limit":
        if not args.limit_price:
            sys.exit("--limit-price required for limit orders")
        return limit_order(args.action, args.qty, args.limit_price)
    elif ot == "stop":
        if not args.stop_price:
            sys.exit("--stop-price required for stop orders")
        return stop_order(args.action, args.qty, args.stop_price)
    elif ot == "stop_limit":
        if not args.limit_price or not args.stop_price:
            sys.exit("--limit-price and --stop-price required for stop_limit orders")
        return stop_limit_order(args.action, args.qty, args.limit_price, args.stop_price)
    elif ot == "bracket":
        for required in ("entry_price", "take_profit", "stop_loss"):
            if not getattr(args, required):
                sys.exit(f"--{required.replace('_','-')} required for bracket orders")
        return bracket_order(args.action, args.qty, args.entry_price,
                             args.take_profit, args.stop_loss, ib)
    else:
        sys.exit(f"Unknown order type: {ot}")


async def main() -> None:
    args = parse_args()
    ib = await connect(args.host, args.port, args.client_id)

    try:
        print("\n--- Account Summary ---")
        await print_account_summary(ib)

        print("\n--- Positions ---")
        await print_positions(ib)

        print("\n--- Open Orders ---")
        await print_open_orders(ib)

        if args.info_only:
            return

        if not args.symbol or not args.action or not args.qty:
            sys.exit("--symbol, --action, and --qty are required to place an order")

        contract = build_contract(args)
        order = build_order(args, ib)

        print(f"\nPlacing {args.order_type.upper()} order ...")
        if isinstance(order, list):          # BracketOrder is a list of Orders
            await place_bracket(ib, contract, order)
        else:
            await place(ib, contract, order)

    finally:
        disconnect(ib)


if __name__ == "__main__":
    asyncio.run(main())

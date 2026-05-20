"""
IB Order Manager – Web Interface
==================================
A browser-based dashboard for Interactive Brokers account management and order entry.

Requirements:
    pip install fastapi "uvicorn[standard]" ib_insync

Run:
    python app.py
    Then open http://localhost:8000 in your browser.

IB Gateway must be running with API connections enabled.
Ports:  IB Gateway paper=4002  |  IB Gateway live=4001
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional
try:
    from zoneinfo import ZoneInfo
    _EASTERN = ZoneInfo("America/New_York")
except KeyError:
    # tzdata package not installed (common on Windows); fall back to fixed offset.
    # Install tzdata: pip install tzdata
    from datetime import timezone
    _EASTERN = timezone(timedelta(hours=-5))

# ib_insync (via eventkit) calls asyncio.get_event_loop() at import time.
# On Python 3.12+ that raises RuntimeError when no loop is set, so we
# create a temporary one just to satisfy the import.  We clear it
# immediately afterwards so uvicorn can install its own loop cleanly.
asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import (
    IB,
    Stock, Option, Future, Forex, CFD,
    MarketOrder, LimitOrder, StopOrder, StopLimitOrder,
    util,
)

asyncio.set_event_loop(None)   # hand loop ownership back to uvicorn

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _next_day_1pm_eastern() -> str:
    """Return IB-format GTD string for 1 pm Eastern today.

    IB goodTillDate format: "YYYYMMDD HH:MM:SS {tz}"
    e.g. "20240116 13:00:00 US/Eastern"
    """
    now = datetime.now(tz=_EASTERN)
    return f"{now.strftime('%Y%m%d')} 13:00:00 US/Eastern"

# ---------------------------------------------------------------------------
# Global IB instance
# IB() must be created *inside* uvicorn's running loop (lifespan below),
# otherwise its internal asyncio primitives bind to the wrong loop and you
# get "Future attached to a different loop" errors.
# ---------------------------------------------------------------------------
ib: IB  # assigned in lifespan startup

# ---------------------------------------------------------------------------
# Auto-close state
# When enabled: at 15:57 ET all open orders are cancelled;
#               at 15:58 ET all positions are closed at market price.
# ---------------------------------------------------------------------------
autoclose_enabled: bool = True
_cancel_orders_fired_date   = None   # tracks 15:57 cancel-orders fire per calendar date
_close_positions_fired_date = None   # tracks 15:58 close-positions fire per calendar date


async def _cancel_all_open_orders() -> list[dict]:
    """Cancel every pending open order. Returns summary list."""
    await ib.reqAllOpenOrdersAsync()
    PENDING = {"PendingSubmit", "PendingCancel", "PreSubmitted", "Submitted"}
    results = []
    for t in ib.trades():
        if t.orderStatus.status not in PENDING:
            continue
        ib.cancelOrder(t.order)
        results.append({
            "order_id": t.order.orderId,
            "symbol":   t.contract.symbol,
        })
    if results:
        await asyncio.sleep(1)
    return results


async def _close_all_positions_market() -> list[dict]:
    """Close every open position with a market order. Returns summary list."""
    port = ib.portfolio()
    results = []
    for p in port:
        if p.position == 0:
            continue
        action = "SELL" if p.position > 0 else "BUY"
        qty    = abs(p.position)
        order  = MarketOrder(action, qty)
        trade  = ib.placeOrder(p.contract, order)
        results.append({
            "symbol":   p.contract.symbol,
            "action":   action,
            "quantity": qty,
            "order_id": trade.order.orderId,
        })
    if results:
        await asyncio.sleep(1)
    return results


async def _autoclose_loop():
    """Background task: at 15:57 ET cancel all open orders, at 15:58 ET close all positions."""
    global _cancel_orders_fired_date, _close_positions_fired_date
    while True:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            return          # lifespan is shutting down — exit cleanly
        if not autoclose_enabled:
            continue
        if not ib.isConnected():
            continue
        now   = datetime.now(tz=_EASTERN)
        today = now.date()
        # 15:57 — cancel all unexecuted orders
        if now.hour == 15 and now.minute == 57 and _cancel_orders_fired_date != today:
            _cancel_orders_fired_date = today
            await _cancel_all_open_orders()
        # 15:58 — close all positions at market
        if now.hour == 15 and now.minute == 58 and _close_positions_fired_date != today:
            _close_positions_fired_date = today
            await _close_all_positions_market()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ib
    # On Python 3.12+ uvicorn uses asyncio.Runner which never calls
    # asyncio.set_event_loop(), so the thread has no "current" loop even
    # though one is actively running.  ib_insync's synchronous internals call
    # asyncio.get_event_loop() and crash with "no current event loop".
    # Fix: register the already-running loop as the thread's current loop.
    asyncio.set_event_loop(asyncio.get_running_loop())
    ib = IB()
    ac_task = asyncio.create_task(_autoclose_loop())
    yield
    # ── Graceful shutdown ──────────────────────────────────────────────
    ac_task.cancel()
    try:
        await asyncio.wait_for(ac_task, timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    try:
        if ib.isConnected():
            ib.disconnect()
    except Exception:
        pass


app = FastAPI(title="IB Order Manager", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ConnectRequest(BaseModel):
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 1


class OrderRequest(BaseModel):
    symbol: str
    sec_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    action: str                         # BUY | SELL
    quantity: float
    order_type: str = "bracket"         # market | limit | stop | stop_limit | bracket
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    entry_price: Optional[float] = None
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    cancel_next_day_1pm: bool = True    # GTD: cancel at 1 pm Eastern next day (bracket default)
    # Options / Futures only
    expiry: Optional[str] = None        # YYYYMMDD
    strike: Optional[float] = None
    right: Optional[str] = None         # C | P


class ModifyRequest(BaseModel):
    lmt_price: Optional[float] = None
    quantity: Optional[float] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_contract(req: OrderRequest):
    st = req.sec_type.upper()
    sym = req.symbol.upper()
    if st == "STK":
        return Stock(sym, req.exchange, req.currency)
    if st == "OPT":
        if not (req.expiry and req.strike is not None and req.right):
            raise HTTPException(400, "expiry, strike, and right are required for OPT")
        return Option(sym, req.expiry, req.strike, req.right, req.exchange, currency=req.currency)
    if st == "FUT":
        if not req.expiry:
            raise HTTPException(400, "expiry is required for FUT")
        return Future(sym, req.expiry, req.exchange, currency=req.currency)
    if st == "CASH":
        return Forex(sym)
    if st == "CFD":
        return CFD(sym, req.exchange, req.currency)
    raise HTTPException(400, f"Unknown sec_type: {req.sec_type}")


# ---------------------------------------------------------------------------
# Connection endpoints
# ---------------------------------------------------------------------------

@app.post("/api/connect")
async def connect(req: ConnectRequest):
    try:
        if ib.isConnected():
            ib.disconnect()
        await ib.connectAsync(req.host, req.port, clientId=req.client_id)
        # Allow ib_insync's auto-subscriptions (reqAccountUpdates, reqPositions)
        # time to receive their first data batch from IB.
        await asyncio.sleep(2)
        return {"status": "connected", "host": req.host, "port": req.port, "client_id": req.client_id}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/disconnect")
async def disconnect():
    ib.disconnect()
    return {"status": "disconnected"}


@app.get("/api/status")
async def status():
    return {"connected": ib.isConnected()}


# ---------------------------------------------------------------------------
# Account / data endpoints
# ---------------------------------------------------------------------------

@app.get("/api/account")
async def account():
    if not ib.isConnected():
        raise HTTPException(400, "Not connected to IB")
    try:
        # ib.accountValues() is the cached data from ib_insync's auto-subscription
        # (reqAccountUpdates).  It contains ALL account tags including P&L.
        # Poll briefly in case we were called right after connect.
        vals = ib.accountValues()
        if not vals:
            await asyncio.sleep(1)
            vals = ib.accountValues()

        # Build result dict: one entry per tag.
        # When IB sends the same tag in multiple currencies (USD, EUR, BASE…)
        # prefer the BASE entry, then empty-string (account base ccy), then USD.
        result: dict = {}
        PREF = {"BASE": 0, "": 1, "USD": 2}
        for item in vals:
            cur  = item.currency or ""
            prev = result.get(item.tag)
            if prev is None or PREF.get(cur, 99) < PREF.get(prev["currency"] or "", 99):
                result[item.tag] = {"value": item.value, "currency": cur}

        return result
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/debug/account")
async def debug_account():
    """Raw dump — open in browser to see every tag IB is sending."""
    if not ib.isConnected():
        raise HTTPException(400, "Not connected to IB")
    vals = ib.accountValues()
    return [{"tag": v.tag, "value": v.value, "currency": v.currency} for v in vals]


@app.get("/api/positions")
async def positions():
    if not ib.isConnected():
        raise HTTPException(400, "Not connected to IB")
    try:
        # ib.portfolio() is the cached portfolio from ib_insync's auto-subscription.
        # Avoids positionsAsync() which makes a new IB API call and has
        # compatibility issues on Python 3.12+.  Also has richer data
        # (market price, market value, unrealized/realized P&L).
        port = ib.portfolio()
        if not port:
            await asyncio.sleep(1)
            port = ib.portfolio()

        return [
            {
                "account":        p.account,
                "symbol":         p.contract.symbol,
                "sec_type":       p.contract.secType,
                "exchange":       p.contract.exchange,
                "currency":       p.contract.currency,
                "position":       float(p.position),
                "avg_cost":       float(p.averageCost),
                "market_price":   float(p.marketPrice),
                "market_value":   float(p.marketValue),
                "unrealized_pnl": float(p.unrealizedPNL),
                "realized_pnl":   float(p.realizedPNL),
            }
            for p in port
        ]
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ---------------------------------------------------------------------------
# Auto-close endpoints
# ---------------------------------------------------------------------------

class AutoCloseRequest(BaseModel):
    enabled: bool


@app.get("/api/autoclose")
async def get_autoclose():
    now = datetime.now(tz=_EASTERN)
    return {
        "autoclose_enabled":           autoclose_enabled,
        "server_time_eastern":         now.strftime("%H:%M:%S"),
        "cancel_orders_time":          "15:57",
        "close_positions_time":        "15:58",
        "cancel_orders_fired_date":    str(_cancel_orders_fired_date)   if _cancel_orders_fired_date   else None,
        "close_positions_fired_date":  str(_close_positions_fired_date) if _close_positions_fired_date else None,
    }


@app.post("/api/autoclose")
async def set_autoclose(req: AutoCloseRequest):
    global autoclose_enabled
    autoclose_enabled = req.enabled
    return {"autoclose_enabled": autoclose_enabled}


@app.post("/api/autoclose/trigger")
async def trigger_autoclose_now():
    """Manually trigger an immediate market-close of all positions (for testing)."""
    if not ib.isConnected():
        raise HTTPException(400, "Not connected to IB")
    try:
        results = await _close_all_positions_market()
        return {"closed": results}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/orders")
async def open_orders():
    if not ib.isConnected():
        raise HTTPException(400, "Not connected to IB")
    try:
        # reqAllOpenOrdersAsync refreshes orders from all connected clients.
        await ib.reqAllOpenOrdersAsync()

        PENDING_STATUSES = {"PendingSubmit", "PendingCancel", "PreSubmitted", "Submitted"}
        result = []
        for t in ib.trades():
            o = t.order
            s = t.orderStatus
            if s.status not in PENDING_STATUSES:
                continue
            result.append({
                "order_id":   o.orderId,
                "symbol":     t.contract.symbol,
                "sec_type":   t.contract.secType,
                "action":     o.action,
                "quantity":   float(o.totalQuantity),
                "order_type": o.orderType,
                "tif":        o.tif,
                "lmt_price":  float(o.lmtPrice) if o.lmtPrice else None,
                "aux_price":  float(o.auxPrice) if o.auxPrice else None,
                "status":     s.status,
                "filled":     float(s.filled),
                "remaining":  float(s.remaining),
            })
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ---------------------------------------------------------------------------
# Order management endpoints
# ---------------------------------------------------------------------------

@app.post("/api/orders")
async def place_order(req: OrderRequest):
    if not ib.isConnected():
        raise HTTPException(400, "Not connected to IB")
    try:
        contract = _build_contract(req)
        ot = req.order_type.lower()

        if ot == "bracket":
            if not all([req.entry_price is not None, req.take_profit is not None, req.stop_loss is not None]):
                raise HTTPException(400, "entry_price, take_profit, stop_loss required for bracket orders")
            bracket = ib.bracketOrder(
                req.action.upper(), req.quantity,
                req.entry_price, req.take_profit, req.stop_loss,
            )
            # bracket[0] = parent (entry), bracket[1] = take-profit, bracket[2] = stop-loss
            bracket[1].tif = "DAY"
            bracket[2].tif = "DAY"
            if req.cancel_next_day_1pm:
                # Cancel the entry if unfilled by 1 pm Eastern today.
                bracket[0].tif = "GTD"
                bracket[0].goodTillDate = _next_day_1pm_eastern()
            else:
                bracket[0].tif = "DAY"
            trades = [ib.placeOrder(contract, o) for o in bracket]
            await asyncio.sleep(1)
            return [{"order_id": t.order.orderId, "status": t.orderStatus.status} for t in trades]

        if ot == "market":
            order = MarketOrder(req.action.upper(), req.quantity)
        elif ot == "limit":
            if req.limit_price is None:
                raise HTTPException(400, "limit_price required for limit orders")
            order = LimitOrder(req.action.upper(), req.quantity, req.limit_price)
            order.tif = "DAY"
        elif ot == "stop":
            if req.stop_price is None:
                raise HTTPException(400, "stop_price required for stop orders")
            order = StopOrder(req.action.upper(), req.quantity, req.stop_price)
            order.tif = "DAY"
        elif ot == "stop_limit":
            if req.limit_price is None or req.stop_price is None:
                raise HTTPException(400, "limit_price and stop_price required for stop_limit orders")
            order = StopLimitOrder(req.action.upper(), req.quantity, req.limit_price, req.stop_price)
            order.tif = "DAY"
        else:
            raise HTTPException(400, f"Unknown order type: {req.order_type}")

        trade = ib.placeOrder(contract, order)
        await asyncio.sleep(1)
        return {
            "order_id": trade.order.orderId,
            "status":   trade.orderStatus.status,
            "action":   trade.order.action,
            "quantity": float(trade.order.totalQuantity),
            "symbol":   contract.symbol,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.delete("/api/orders/{order_id}")
async def cancel_order(order_id: int):
    if not ib.isConnected():
        raise HTTPException(400, "Not connected to IB")
    for trade in ib.openTrades():
        if trade.order.orderId == order_id:
            ib.cancelOrder(trade.order)
            await asyncio.sleep(1)
            return {"order_id": order_id, "status": trade.orderStatus.status}
    raise HTTPException(404, f"Order {order_id} not found")


@app.put("/api/orders/{order_id}")
async def modify_order(order_id: int, req: ModifyRequest):
    if not ib.isConnected():
        raise HTTPException(400, "Not connected to IB")
    for trade in ib.openTrades():
        if trade.order.orderId == order_id:
            o = trade.order
            if req.lmt_price is not None:
                o.lmtPrice = req.lmt_price
            if req.quantity is not None:
                o.totalQuantity = req.quantity
            ib.placeOrder(trade.contract, o)
            await asyncio.sleep(1)
            return {"order_id": order_id, "status": trade.orderStatus.status}
    raise HTTPException(404, f"Order {order_id} not found")


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
os.makedirs("static", exist_ok=True)
app.mount("/", StaticFiles(directory="static", html=True), name="static")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        timeout_graceful_shutdown=5,   # force exit 5 s after Ctrl+C
    )

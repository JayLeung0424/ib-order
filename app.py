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
import json
import os
import time
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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
    Order, MarketOrder, LimitOrder, StopOrder, StopLimitOrder,
    util,
)

asyncio.set_event_loop(None)   # hand loop ownership back to uvicorn

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from collections import deque


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _next_day_1pm_eastern() -> str:
    """Return IB-format GTD string for 12:30 pm Eastern on the next trading day.

    Skips weekends (Sat/Sun).  Note: does not skip US market holidays; on
    holidays IB will still accept the order but it will sit until the next
    session.

    IB goodTillDate format: "YYYYMMDD HH:MM:SS {tz}"
    e.g. "20240116 12:30:00 US/Eastern"
    """
    from datetime import timedelta as _td
    now = datetime.now(tz=_EASTERN)
    cutoff_date = now.date()
    # If we're already past 12:30 ET today, start from tomorrow.
    if now.hour > 12 or (now.hour == 12 and now.minute >= 30):
        cutoff_date = cutoff_date + _td(days=1)
    # Skip Saturday (5) and Sunday (6): advance to Monday.
    while cutoff_date.weekday() >= 5:
        cutoff_date = cutoff_date + _td(days=1)
    return f"{cutoff_date.strftime('%Y%m%d')} 12:30:00 US/Eastern"


def _next_trading_day_1555_et() -> str:
    """Return IB-format GoodAfterTime string for 15:55 Eastern on the next trading day.

    Used by the Market-on-Close substitute order (a regular MARKET order with
    GoodAfterTime) so that it activates near market close and executes at
    market, instead of using MOC which cannot join an OCA group.

    Format: "YYYYMMDD HH:MM:SS {tz}"
    """
    from datetime import timedelta as _td
    now = datetime.now(tz=_EASTERN)
    target_date = now.date()
    # If we're already past 15:55 ET today, schedule for tomorrow.
    if now.hour > 15 or (now.hour == 15 and now.minute >= 55):
        target_date = target_date + _td(days=1)
    # Skip Saturday (5) and Sunday (6): advance to Monday.
    while target_date.weekday() >= 5:
        target_date = target_date + _td(days=1)
    return f"{target_date.strftime('%Y%m%d')} 15:55:00 US/Eastern"


# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------
# Configure via environment variables:
#   TG_BOT_TOKEN  – Bot token from @BotFather
#   TG_CHAT_ID    – Target chat/channel id (int, or -100… for channels)
# If either is missing, notifications are silently skipped.

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")


def _tg_send(text: str) -> None:
    """Send a message to Telegram.  Runs in a worker thread."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id":    TG_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as exc:
        print(f"[tg] send failed: {exc}")


def _fmt_price(p: float) -> str:
    """Format a price: trim trailing zeros for clean display."""
    if p is None:
        return "0"
    f = float(p)
    s = f"{f:,.2f}"
    return s


def _fmt_diff(d: float) -> str:
    """Format a signed price difference, e.g. '+20' / '-40' (rounded to int)."""
    if d is None:
        d = 0
    f = round(float(d))
    sign = "+" if f >= 0 else "-"
    return f"{sign}{abs(f):,}"


def _format_bracket_line(contract, entry, tp, sl, close) -> str:
    """Multi-line bracket summary.

        AAPL (EOD - 15:55)
        BUY 5 @160 200(+1,560.00) 120(-1,560.00)

    P&L = (leg - entry) * qty * 7.8  (sign flipped for SELL/short entries),
    matching the open-orders TP/SL P&L display.
    """
    sym    = getattr(contract, "symbol", "") or ""
    action = (getattr(entry, "action", "") or "").upper()
    qty    = float(getattr(entry, "totalQuantity", 0) or 0)
    e_px   = float(getattr(entry, "lmtPrice", 0) or 0)
    tp_px  = float(getattr(tp, "lmtPrice", 0) or 0)
    sl_px  = float(getattr(sl, "auxPrice", 0) or 0)

    # EOD time from the close leg's GoodAfterTime, e.g.
    # "20260116 15:55:00 US/Eastern" -> "15:55"
    eod = ""
    if close is not None:
        gat = getattr(close, "goodAfterTime", "") or ""
        parts = gat.split()
        if len(parts) >= 2:
            t = parts[1]
            eod = t[:5] if len(t) >= 5 else t

    _FX = 7.8  # USD->HKD conversion / contract multiplier used by open-orders view
    buy_entry = action == "BUY"

    def leg_pnl(leg_price: float) -> float:
        raw = (leg_price - e_px) if buy_entry else (e_px - leg_price)
        return raw * qty * _FX

    tp_pnl = leg_pnl(tp_px)
    sl_pnl = leg_pnl(sl_px)

    header = f"{sym} (EOD - {eod})" if eod else sym
    body   = (
        f"{action} {int(qty)} @{_fmt_price(e_px)}\n"
        f"TP {_fmt_price(tp_px)}({_fmt_diff(tp_pnl)})\n"
        f"SL {_fmt_price(sl_px)}({_fmt_diff(sl_pnl)})"
    )
    return f"{header}\n{body}"


def _format_single_line(contract, order) -> str:
    """One-line summary for a non-bracket order."""
    sym    = getattr(contract, "symbol", "") or ""
    action = getattr(order, "action", "") or ""
    qty    = float(getattr(order, "totalQuantity", 0) or 0)
    otype  = (getattr(order, "orderType", "") or "").upper()
    lmt    = float(getattr(order, "lmtPrice", 0) or 0)
    stp    = float(getattr(order, "auxPrice", 0) or 0)
    tif    = getattr(order, "tif", "") or ""

    parts = [sym, action, _fmt_price(qty)]
    if otype == "LMT":
        parts.append(f"LMT @ {_fmt_price(lmt)}")
    elif otype == "STP":
        parts.append(f"STP @ {_fmt_price(stp)}")
    elif otype in ("STP_LMT", "STOP_LIMIT"):
        parts.append(f"STP_LMT {_fmt_price(lmt)} / {_fmt_price(stp)}")
    elif otype == "MKT":
        parts.append("MKT")
    else:
        parts.append(otype)
    if tif and tif != "DAY":
        parts.append(tif)
    return " ".join(parts)


async def _tg_notify_order(contract, order) -> None:
    """Send a Telegram notification describing a placed order."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    body = _format_single_line(contract, order)
    await asyncio.to_thread(_tg_send, body)


async def _tg_notify_orders(contract, orders) -> None:
    """Notify about multiple legs (e.g. a bracket order).

    `orders` is the list [entry, tp, sl, close] as built in place_order().
    Falls back to a per-leg dump if the shape is not a recognisable bracket.
    """
    if not TG_BOT_TOKEN or not TG_CHAT_ID or not orders:
        return

    # Recognise a bracket: entry is a LMT with parentId 0, followed by
    # children with parentId == entry.orderId.  We also expect 4 legs.
    if len(orders) >= 3:
        entry, tp, sl = orders[0], orders[1], orders[2]
        close = orders[3] if len(orders) >= 4 else None
        body = _format_bracket_line(contract, entry, tp, sl, close)
    else:
        body = "\n".join(_format_single_line(contract, o) for o in orders)
    await asyncio.to_thread(_tg_send, body)


# ---------------------------------------------------------------------------
# Global IB instance
# IB() must be created *inside* uvicorn's running loop (lifespan below),
# otherwise its internal asyncio primitives bind to the wrong loop and you
# get "Future attached to a different loop" errors.
# ---------------------------------------------------------------------------
ib: IB  # assigned in lifespan startup
is_paper_account: bool = False   # set after connect; paper accounts use a test schedule

# ---------------------------------------------------------------------------
# Local fill log
# ---------------------------------------------------------------------------
# IB's reqExecutions API only returns *today's* fills (per the official docs:
# "Only the current day's executions can be retrieved").  Opening fills from
# previous days are therefore not retrievable through the API, which makes
# overnight-trade detection impossible unless we keep our own record.
#
# To bridge this, every fill observed by the running app (via the live
# execDetailsEvent) is appended to a JSON Lines file.  The overnight-trades
# endpoint then merges the on-disk history with today's reqExecutions() result
# and FIFO-matches them per (symbol, secType, currency).
import threading
_FILLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fills.jsonl")
_fills_lock = threading.Lock()


def _fut_to_dict(f) -> dict:
    """Serialize a Fill to a JSON-safe dict for the local log file."""
    ex = f.execution
    c = f.contract
    # Execution.time can be naive UTC; ensure we have an ISO string.
    t = ex.time
    t_iso = t.isoformat() if hasattr(t, "isoformat") else str(t)
    return {
        "exec_id":   ex.execId,
        "time_utc":  t_iso,
        "symbol":    c.symbol,
        "sec_type":  c.secType,
        "currency":  c.currency or "",
        "exchange":  c.exchange or "",
        "side":      ex.side or "",
        "shares":    float(ex.shares),
        "price":     float(ex.price),
        "order_id":  ex.orderId,
        "perm_id":   ex.permId,
        "client_id": ex.clientId,
    }


def _fill_to_dto(f):
    """Rebuild a dict (used by FIFO matcher) from a JSON-recorded fill."""
    return {
        "exec_id":  f["exec_id"],
        "time":     f["time_utc"],     # ISO string with tz
        "symbol":   f["symbol"],
        "sec_type": f["sec_type"],
        "currency": f["currency"],
        "side":     f["side"],
        "shares":   f["shares"],
        "price":    f["price"],
    }


def _record_fill(f):
    """Append a live fill to the local JSONL file (idempotent by execId)."""
    d = _fut_to_dict(f)
    if not d["exec_id"]:
        return
    with _fills_lock:
        existing = set()
        try:
            with open(_FILLS_FILE, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        existing.add(__import__("json").loads(line)["exec_id"])
                    except Exception:
                        pass
        except FileNotFoundError:
            pass
        if d["exec_id"] in existing:
            return
        with open(_FILLS_FILE, "a", encoding="utf-8") as fh:
            fh.write(__import__("json").dumps(d) + "\n")


def _load_local_fills(days: int = 60) -> list:
    """Return local fills as dto list, optionally filtered by lookback days."""
    import json
    cutoff = datetime.now(tz=_EASTERN) - timedelta(days=max(days or 0, 1))
    out = []
    try:
        with open(_FILLS_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                try:
                    t = datetime.fromisoformat(d["time_utc"])
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                    if t.astimezone(_EASTERN) < cutoff:
                        continue
                except Exception:
                    pass
                out.append(_fill_to_dto(d))
    except FileNotFoundError:
        pass
    return out


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
    # Persist every live fill so overnight-trade matching works across days.
    # IB's reqExecutions only returns same-day fills, so we must keep our own
    # log of yesterday's opening fills to match against today's closing fills.
    ib.execDetailsEvent += lambda trade, fill: _record_fill(fill)
    # Black-swan detector: poll Futu snapshots in the background.
    global _bs_task
    _bs_task = asyncio.create_task(_bs_loop())
    yield
    # ── Graceful shutdown ──────────────────────────────────────────────
    if _bs_task is not None:
        _bs_task.cancel()
        try:
            await _bs_task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        if ib.isConnected():
            ib.disconnect()
    except Exception:
        pass


app = FastAPI(title="IB Order Manager", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Futu integration
# ---------------------------------------------------------------------------
# The futu-api SDK is synchronous and opens a TCP socket per call; we run it
# inside a worker thread so it does not block uvicorn's event loop.

FUTU_QUOTE_HOST = "127.0.0.1"
FUTU_QUOTE_PORT = 11111
FUTU_GROUP = "TOP LIST"
FUTU_EARNINGS_MARKET = "US"

# --- Earnings-day (業績日) block -------------------------------------------
# When the TOP-LIST is fetched we also ask Futu for the US earnings calendar
# and flag any symbol that reports TODAY pre-market (BEFORE) or on the previous
# trading day post-market (AFTER).  Flagged symbols are prohibited from trading
# (enforced server-side in place_order and mirrored in the UI).  Symbols missing
# from Futu's calendar fall back to a per-symbol yfinance earnings check
# (_yf_earn_block).
import re  # threading is already imported at module level above

_EARN_CACHE = {"fetched_at": None, "map": {}}   # norm-symbol -> {"earnings_date","pub_type"}
_EARN_LOCK = threading.Lock()
_EARN_TTL = 300  # seconds
_EARN_RE_COPY = re.compile(r'[\s.\-]+')

# --- yfinance per-symbol fallback ------------------------------------------
# Futu's US earnings calendar sometimes misses symbols entirely (e.g. PLD
# 2026Q3 on 2026-10-15).  yfinance's Ticker.earnings_dates gives a tz-aware
# earnings history per symbol; used as fallback for symbols missing from the
# Futu calendar.  Cached per symbol (success and failure) to respect Yahoo
# rate limits.
_YF_CACHE: dict = {}   # norm-symbol -> {"block": dict|None, "fetched_at": datetime}
_YF_LOCK = threading.Lock()
_YF_TTL = 1800         # seconds


def _yf_earn_block(sym: str, today=None, prev=None):
    """yfinance fallback block info for one symbol, or None.

    Mirrors the Futu literal-day rule: BEFORE (release time < 09:30 ET) on
    today, or AFTER (release time >= 16:00 ET) on the previous trading day.
    Returns {"earnings_date": "YYYY-MM-DD", "pub_type": "BEFORE"|"AFTER"} or
    None.  Results (including misses) are cached for _YF_TTL seconds; when
    today/prev are supplied explicitly the cache is bypassed (testability).
    """
    from datetime import timedelta as _td

    key = _norm_earn_symbol(sym)
    if not key:
        return None
    explicit = today is not None and prev is not None
    if not explicit:
        p, t = _earnings_window_dates()          # (prev, today)
        prev, today = p, t
    now = datetime.now(tz=_EASTERN)
    if not explicit:
        with _YF_LOCK:
            e = _YF_CACHE.get(key)
            if e is not None and (now - e["fetched_at"]).total_seconds() < _YF_TTL:
                b = e["block"]
                return dict(b) if b else None
    block = None
    try:
        import yfinance as yf
        ticker_sym = str(sym).replace(" ", "-")   # IB 'BRK B' -> yfinance 'BRK-B'
        df = yf.Ticker(ticker_sym).earnings_dates
        if df is not None and len(df):
            import pandas as _pd
            for idx in df.index:
                ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
                if ts is None or _pd.isna(ts):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_EASTERN)   # Yahoo times are ET
                ts_et = ts.astimezone(_EASTERN)
                hh = ts_et.hour + ts_et.minute / 60.0
                if hh < 9.5:
                    pt = "BEFORE"
                elif hh >= 16.0:
                    pt = "AFTER"
                else:
                    pt = "REGULAR"
                ed = ts_et.date().isoformat()
                if (pt == "BEFORE" and ed == today) or (pt == "AFTER" and ed == prev):
                    block = {"earnings_date": ed, "pub_type": pt}
                    break
    except Exception as exc:
        print(f"[earnings] yfinance fallback failed for {sym}: {exc}")
        if not explicit:
            with _YF_LOCK:
                _YF_CACHE[key] = {"block": None, "fetched_at": now - _td(seconds=_YF_TTL * 0.9)}
        return None
    if not explicit:
        with _YF_LOCK:
            _YF_CACHE[key] = {"block": block, "fetched_at": now}
    return dict(block) if block else None


def _norm_earn_symbol(sym) -> str:
    """Normalize IB symbol ('BRK B') / futu code ('US.BRK.B') to a comparable key ('BRKB')."""
    return _EARN_RE_COPY.sub("", str(sym or "").upper())


def _earnings_window_dates() -> tuple[str, str]:
    """(prev_trading_day, today) as 'yyyy-MM-dd' in ET, skipping weekends."""
    now = datetime.now(tz=_EASTERN)
    today = now.date()
    prev = today - timedelta(days=1)
    while prev.weekday() >= 5:          # skip Sat(5)/Sun(6)
        prev = prev - timedelta(days=1)
    return (prev.isoformat(), today.isoformat())


def _earrows_fetch(quote_ctx, begin_date, end_date) -> dict:
    """Query Futu's US earnings calendar and return ONLY the blocked symbols.

    Sync; assumes an already-open quote context.  Returns
    {norm-symbol: {"earnings_date", "pub_type"}} and {} on any failure.
    """
    from futu import RET_OK

    try:
        ret, data = quote_ctx.get_earnings_calendar(
            market=FUTU_EARNINGS_MARKET, begin_date=begin_date, end_date=end_date
        )
    except Exception as exc:
        print(f"[earnings] get_earnings_calendar failed: {exc}")
        return {}
    if ret != RET_OK:
        print(f"[earnings] get_earnings_calendar ret != RET_OK: {data}")
        return {}

    prev, today = _earnings_window_dates()
    out: dict = {}
    try:
        records = data.to_dict(orient="records") if hasattr(data, "to_dict") else []
    except Exception as exc:
        print(f"[earnings] parse failed: {exc}")
        return {}
    for row in records:
        try:
            sec = str(row.get("security") or "")
            code = sec[3:] if sec.startswith("US.") else sec
            key = _norm_earn_symbol(code)
            pt = str(row.get("pub_type") or "").upper()
            ed = str(row.get("earnings_date") or "")
            # Literal-day rule: BEFORE on today, or AFTER on the previous
            # trading day.  No widening.
            if (pt == "BEFORE" and ed == today) or (pt == "AFTER" and ed == prev):
                out[key] = {"earnings_date": ed, "pub_type": pt}
        except Exception:
            continue
    return out


def _earnings_block_map() -> dict:
    """Return the cached {norm-symbol: {...}} earnings-block map.

    Sync; safe to call from a worker thread.  Refreshes from Futu when the
    cache is older than _EARN_TTL.  On failure it keeps the previous entries
    and schedules a retry for _EARN_TTL/2 later (never raises).
    """
    now = datetime.now(tz=_EASTERN)
    with _EARN_LOCK:
        fa = _EARN_CACHE.get("fetched_at")
        if fa is not None and (now - fa).total_seconds() < _EARN_TTL:
            return dict(_EARN_CACHE["map"])

    # (Re)build outside the lock so /api/stocks does not serialize on it.
    quote_ctx = None
    m = None
    try:
        from futu import OpenQuoteContext
        prev, today = _earnings_window_dates()
        begin = prev                                   # prev trading day
        end = (datetime.now(tz=_EASTERN).date() + timedelta(days=1)).isoformat()  # today + 1
        quote_ctx = OpenQuoteContext(host=FUTU_QUOTE_HOST, port=FUTU_QUOTE_PORT)
        m = _earrows_fetch(quote_ctx, begin, end)
    except Exception as exc:
        print(f"[earnings] refresh failed: {exc}")
        m = None
    finally:
        if quote_ctx is not None:
            try:
                quote_ctx.close()
            except Exception:
                pass

    with _EARN_LOCK:
        if not m:
            # Failure (exception or empty result): retry later but not
            # instantly; keep any stale entries rather than wiping them.
            _EARN_CACHE["fetched_at"] = now - timedelta(seconds=_EARN_TTL / 2)
            return dict(_EARN_CACHE["map"])
        _EARN_CACHE["fetched_at"] = now
        _EARN_CACHE["map"] = m
        return dict(m)


async def _earnings_block_async() -> dict:
    try:
        await asyncio.to_thread(_earnings_block_map)
    except Exception as exc:
        print(f"[earnings] refresh failed: {exc}")
    return dict(_EARN_CACHE["map"])


def _futu_get_stocks(group_name: str = FUTU_GROUP) -> dict:
    from futu import OpenQuoteContext, UserSecurityGroupType, ModifyUserSecurityOp, RET_OK
    import datetime as _dt

    quote_ctx = OpenQuoteContext(host=FUTU_QUOTE_HOST, port=FUTU_QUOTE_PORT)
    try:
        groups = []
        ret, group_data = quote_ctx.get_user_security_group(group_type=UserSecurityGroupType.ALL)
        if ret == RET_OK and hasattr(group_data, "to_dict"):
            groups = group_data.to_dict(orient="records")
        elif ret == RET_OK and isinstance(group_data, list):
            groups = [
                {"group_name": g.get("group_name", "") if isinstance(g, dict) else str(g),
                 "group_type": g.get("group_type", "") if isinstance(g, dict) else ""}
                for g in group_data
            ]

        ret, data = quote_ctx.get_user_security(group_name)
        if ret != RET_OK:
            return {"ok": False, "error": str(data), "groups": groups}

        codes = data["code"].tolist() if hasattr(data, "columns") and "code" in data.columns else []
        records = []
        if codes:
            snap = {}
            ret_s, snap_data = quote_ctx.get_market_snapshot(codes)
            if ret_s == RET_OK and hasattr(snap_data, "to_dict"):
                for r in snap_data.to_dict(orient="records"):
                    snap[r["code"]] = r

            today = _dt.date.today()
            end = today.strftime("%Y-%m-%d")
            start = (today - _dt.timedelta(days=15)).strftime("%Y-%m-%d")
            for code in codes:
                s = snap.get(code, {})
                today_open = s.get("open_price")
                if today_open is None or float(today_open) == 0:
                    today_open = s.get("last_price")
                name = s.get("name", "")
                anchor = s.get("prev_close_price")
                prev_open = prev_high = prev_low = None
                prev_close = anchor
                prev_volume = None
                try:
                    ret_k, kl, _ = quote_ctx.request_history_kline(
                        code, start=start, end=end, max_count=20
                    )
                    if ret_k == RET_OK and len(kl):
                        rows = kl.to_dict(orient="records")
                        prev = None
                        if anchor is not None:
                            for r in reversed(rows):
                                cv = r.get("close")
                                if cv is not None and abs(float(cv) - float(anchor)) < 1e-6:
                                    prev = r
                                    break
                        if prev is None and len(rows) >= 2:
                            prev = rows[-2]
                        elif prev is None:
                            prev = rows[-1]
                        prev_open = prev.get("open")
                        prev_high = prev.get("high")
                        prev_low = prev.get("low")
                        prev_close = prev.get("close")
                        prev_volume = prev.get("volume")
                except Exception:
                    pass
                records.append({
                    "code": code,
                    "name": name,
                    "prev_open": prev_open,
                    "prev_high": prev_high,
                    "prev_low": prev_low,
                    "prev_close": prev_close,
                    "prev_volume": prev_volume,
                    "today_open": today_open,
                })

        try:
            earn_map = _earnings_block_map()
        except Exception as exc:
            print(f"[earnings] block map failed: {exc}")
            earn_map = {}
        for r in records:
            e = earn_map.get(_norm_earn_symbol(str(r.get("code", "")).replace("US.", "")))
            r["earn_blocked"] = e is not None
            r["earn_date"] = (e or {}).get("earnings_date")
            r["earn_pub_type"] = (e or {}).get("pub_type")

        for r in records:
            if r.get("earn_blocked"):
                continue
            fb = _yf_earn_block(str(r.get("code", "")).replace("US.", ""))
            if fb:
                r["earn_blocked"] = True
                r["earn_date"] = fb["earnings_date"]
                r["earn_pub_type"] = fb["pub_type"]

        return {"ok": True, "stocks": records, "groups": groups, "group": group_name}
    finally:
        quote_ctx.close()


def _futu_add_stocks(group_name: str, code_list: list) -> dict:
    from futu import OpenQuoteContext, ModifyUserSecurityOp, RET_OK
    full_list = ["US." + c for c in code_list]
    # 類股代號從 screener／紙front 來係連字符（BF-B = Berkshire B，舊 BRK-B 同 BRK.A→BF.A 一樣）；
    # futu user-security 用點號（US.BF.B / US.BRK.B），連字符會報「未知股票」。
    futu_list = ["US." + c.replace("-", ".") for c in code_list]
    quote_ctx = OpenQuoteContext(host=FUTU_QUOTE_HOST, port=FUTU_QUOTE_PORT)
    try:
        ret, data = quote_ctx.get_user_security(group_name)
        if ret == RET_OK:
            existing = data["code"].tolist() if hasattr(data, "columns") and "code" in data.columns else []
            if existing:
                quote_ctx.modify_user_security(group_name, ModifyUserSecurityOp.DEL, existing)
        ret, err = quote_ctx.modify_user_security(
            group_name, ModifyUserSecurityOp.ADD, list(reversed(futu_list))
        )
        if ret != RET_OK:
            # futu 成批拒絕：從 error 抽出唔識嘅代碼，剔走後重試，唔會因一隻卡住全部
            _e = str(err)
            bad = {b.upper().strip() for b in re.findall(r"(?:未知股票|unknown stock[^\s]*)\s*([\w.\-/]+)", _e)}
            if bad:
                def _norm(b):
                    return str(b).upper().replace("US.", "").replace(".", "-")
                bad_keys = {_norm(b) for b in bad}
                kept_futu, kept = [], []
                for c, f in zip(code_list, futu_list):
                    if _norm(c) not in bad_keys:
                        kept.append("US." + c)
                        kept_futu.append(f)
                if kept:
                    # 重冇以下事故重疊/重複：reload 現有 group → DEL 晒 → ADD kept
                    ret_c, data_c = quote_ctx.get_user_security(group_name)
                    if ret_c == RET_OK:
                        cur = data_c["code"].tolist() if hasattr(data_c, "columns") and "code" in data_c.columns else []
                        if cur:
                            quote_ctx.modify_user_security(group_name, ModifyUserSecurityOp.DEL, cur)
                    ret2, err2 = quote_ctx.modify_user_security(
                        group_name, ModifyUserSecurityOp.ADD, list(reversed(kept_futu))
                    )
                    if ret2 == RET_OK:
                        return {"ok": True, "codes": kept, "skipped": sorted(bad_keys)}
            return {"ok": False, "error": _e}
        return {"ok": True, "codes": full_list}
    finally:
        quote_ctx.close()


@app.get("/api/stocks")
async def api_stocks(request: Request):
    group = request.query_params.get("group", FUTU_GROUP)
    try:
        return await asyncio.to_thread(_futu_get_stocks, group)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


def _futu_get_quotes(group_name: str = FUTU_GROUP) -> dict:
    """Lightweight: snapshot only (today open / last price), no history klines."""
    from futu import OpenQuoteContext, RET_OK

    quote_ctx = OpenQuoteContext(host=FUTU_QUOTE_HOST, port=FUTU_QUOTE_PORT)
    try:
        ret, data = quote_ctx.get_user_security(group_name)
        if ret != RET_OK:
            return {"ok": False, "error": str(data)}
        codes = data["code"].tolist() if hasattr(data, "columns") and "code" in data.columns else []
        quotes = []
        if codes:
            ret_s, snap = quote_ctx.get_market_snapshot(codes)
            if ret_s == RET_OK and hasattr(snap, "to_dict"):
                for r in snap.to_dict(orient="records"):
                    today_open = r.get("open_price")
                    if today_open is None or float(today_open) == 0:
                        today_open = r.get("last_price")
                    quotes.append({"code": r.get("code"), "today_open": today_open})
        return {"ok": True, "quotes": quotes}
    finally:
        quote_ctx.close()


@app.get("/api/stocks/quotes")
async def api_stock_quotes(request: Request):
    group = request.query_params.get("group", FUTU_GROUP)
    try:
        return await asyncio.to_thread(_futu_get_quotes, group)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


class FutuAddRequest(BaseModel):
    code: str


@app.post("/api/stocks/add")
async def api_add_stock(req: FutuAddRequest):
    code = (req.code or "").strip().upper()
    if not code:
        raise HTTPException(400, "code is required")
    code_list = [c for c in code.replace(",", " ").split() if c]
    if not code_list:
        raise HTTPException(400, "code is required")
    try:
        return await asyncio.to_thread(_futu_add_stocks, FUTU_GROUP, code_list)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Black-swan detector (once-daily market-regime check → block ALL orders)
# ---------------------------------------------------------------------------
# A lightweight circuit breaker.  Once per trading day — at 09:29 ET, one
# minute before the US cash open — a set of market conditions is evaluated
# via yfinance (free, already a dependency; Futu OpenAPI does not cover US
# futures/index quotes).  If ANY condition fires, a black-swan alert is
# raised — this is ADVISORY ONLY: orders are NOT blocked.  The alert is
# surfaced via Telegram and the UI (banner + status pill) and the user
# decides whether to place orders.
#
# Conditions (configurable via env):
#   1. NQ futures gap (last close vs prev close)  |move| > BS_NQ_GAP_PCT   (1.5 %)
#   2. VIX level > BS_VIX_LEVEL (18) AND overnight rise > BS_VIX_MOVE_PCT (5 %)
#   3. Crude oil (CL=F) overnight move |move| > BS_OIL_MOVE_PCT           (3 %)
#   4. Nikkei (^N225) OR HSI (^HSI) overnight move > BS_ASIA_MOVE_PCT     (2 %)
#   5. S&P 500 (^GSPC) own-day move |move| > BS_SPX_MOVE_PCT              (1.5 %)
# TWO-TIER BLACK SWAN (same blocking either way):
#   hits >= BS_MIN_HITS (3)  → "normal"  block (orders disabled)
#   hits >= BS_SEVERE_HITS(4) → "severe" block (orders disabled + red alerts)
# The block auto-clears at the 16:00 ET cash close (see _bs_loop); the next
# check runs at 09:29 ET the following trading day.

BS_CHECK_TIME = os.environ.get("BS_CHECK_TIME", "09:29")   # once-daily check
BS_CHECK_HOUR, BS_CHECK_MIN = (int(x) for x in BS_CHECK_TIME.split(":"))
BS_CLOSE_HOUR, BS_CLOSE_MIN = 16, 0                        # 16:00 ET cash close
# If the server starts/shortly restarts after the check time, a start within
# this grace (seconds) still counts as today's check instead of skipping it.
BS_CHECK_GRACE = int(os.environ.get("BS_CHECK_GRACE", "300"))

BS_NQ_GAP_PCT   = float(os.environ.get("BS_NQ_GAP_PCT",   "1.5"))
BS_VIX_LEVEL    = float(os.environ.get("BS_VIX_LEVEL",    "18"))
BS_VIX_MOVE_PCT = float(os.environ.get("BS_VIX_MOVE_PCT", "5"))
BS_OIL_MOVE_PCT = float(os.environ.get("BS_OIL_MOVE_PCT", "3"))
BS_ASIA_MOVE_PCT= float(os.environ.get("BS_ASIA_MOVE_PCT","2"))
BS_SPX_MOVE_PCT = float(os.environ.get("BS_SPX_MOVE_PCT", "1.5"))
BS_MIN_HITS     = int(os.environ.get("BS_MIN_HITS",     "3"))  # >=3 of 5 → black swan
BS_SEVERE_HITS  = int(os.environ.get("BS_SEVERE_HITS",  "4"))  # >=4 of 5 → severe tier
# Legacy: only relevant when checks ran repeatedly; with once-daily checks it
# simply guarantees each new day's check re-evaluates from scratch.
BLACK_SWAN_CALM = int(os.environ.get("BLACK_SWAN_CALM", "300"))


def _bs_next_check_dt(now: Optional[datetime] = None) -> datetime:
    """Next scheduled once-daily check datetime in ET (today 09:29 if still
    ahead or within the grace window, else tomorrow 09:29)."""
    now = now or datetime.now(tz=_EASTERN)
    target = now.replace(hour=BS_CHECK_HOUR, minute=BS_CHECK_MIN,
                         second=0, microsecond=0)
    if now > target + timedelta(seconds=BS_CHECK_GRACE):
        target += timedelta(days=1)
    return target


def _bs_next_close_dt(now: Optional[datetime] = None) -> datetime:
    """Next 16:00 ET cash close (today if still ahead, else tomorrow)."""
    now = now or datetime.now(tz=_EASTERN)
    target = now.replace(hour=BS_CLOSE_HOUR, minute=BS_CLOSE_MIN,
                         second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target


def _bs_clear_block_locked() -> None:
    """Reset the block state; caller must hold _bs_lock."""
    _bs_state["blocked"] = False
    _bs_state["severity"] = "normal"
    _bs_state["since_et"] = None
    _bs_state["last_hit_ts"] = None
    _bs_state["details"] = []

_bs_lock = threading.Lock()
_bs_state = {
    "blocked":       False,
    "severity":      "normal",   # "normal" (>=3 hits) | "severe" (>=4 hits)
    "since_et":      None,       # datetime the block first engaged
    "last_check_et": None,       # datetime of the most recent check
    "hits_count":    0,          # how many conditions hit last check
    "observations":  [],         # all 4 conditions' current readings
    "details":       [],         # [{"label", "pct", "value", "prev"}]
    "last_hit_ts":   None,       # monotonic ts of the last triggering check
}
_bs_task: Optional["asyncio.Task"] = None


def _bs_is_blocked() -> bool:
    with _bs_lock:
        return _bs_state["blocked"]


def _bs_day_change(sym: str):
    """Return (last_close, prev_close) from yfinance daily history, or None."""
    try:
        import yfinance as yf
        df = yf.Ticker(sym).history(period="5d", interval="1d")
        if df is None:
            return None
        closes = df["Close"].dropna()
        if len(closes) >= 2:
            return float(closes.iloc[-1]), float(closes.iloc[-2])
    except Exception as exc:
        print(f"[blackswan] fetch {sym} failed: {exc}")
    return None


def _bs_calc_pct(name, last, prev, hit=False):
    """Build an observation entry {label, pct, value, prev, hit}; None-safe."""
    pct = ((last - prev) / prev * 100.0) if prev else None
    return {"label": name, "pct": pct, "value": last, "prev": prev, "hit": hit}


def _bs_check_conditions() -> tuple:
    """Evaluate the four black-swan conditions via yfinance.

    Returns (hits, observations): `hits` is the subset of `observations`
    that triggers the block.  Never raises.
    """
    observations = []

    # 1. NQ futures gap (last vs previous close)
    nq = _bs_day_change("NQ=F")
    if nq:
        pct = (nq[0] - nq[1]) / nq[1] * 100.0 if nq[1] else None
        hit = pct is not None and abs(pct) > BS_NQ_GAP_PCT
        observations.append({
            "label": f"NQ 裂口 {pct:+.2f}%" if pct is not None else "NQ 補口",
            "pct": pct, "value": nq[0], "prev": nq[1], "hit": hit,
        })
    else:
        observations.append({"label": "NQ 裂口（無數據）", "pct": None, "value": None, "prev": None, "hit": False})

    # 2. VIX pre-market level > threshold AND overnight rise > threshold
    vix = _bs_day_change("^VIX")
    if vix:
        pct = (vix[0] - vix[1]) / vix[1] * 100.0 if vix[1] else None
        hit = vix[0] > BS_VIX_LEVEL and pct is not None and pct > BS_VIX_MOVE_PCT
        observations.append({
            "label": f"VIX {vix[0]:.1f}（>{BS_VIX_LEVEL:.0f} 且升 {pct:+.2f}%）" if pct is not None else f"VIX {vix[0]:.1f}",
            "pct": pct, "value": vix[0], "prev": vix[1], "hit": hit,
        })
    else:
        observations.append({"label": "VIX（無數據）", "pct": None, "value": None, "prev": None, "hit": False})

    # 3. Crude oil overnight move
    oil = _bs_day_change("CL=F")
    if oil:
        pct = (oil[0] - oil[1]) / oil[1] * 100.0 if oil[1] else None
        hit = pct is not None and abs(pct) > BS_OIL_MOVE_PCT
        observations.append({
            "label": f"油價 {pct:+.2f}%" if pct is not None else "油價",
            "pct": pct, "value": oil[0], "prev": oil[1], "hit": hit,
        })
    else:
        observations.append({"label": "油價（無數據）", "pct": None, "value": None, "prev": None, "hit": False})

    # 4. Asia markets: Nikkei OR HSI overnight move (either one triggers)
    for label, sym in (("日經", "^N225"), ("恆指", "^HSI")):
        d = _bs_day_change(sym)
        if d:
            pct = (d[0] - d[1]) / d[1] * 100.0 if d[1] else None
            hit = pct is not None and abs(pct) > BS_ASIA_MOVE_PCT
            observations.append({
                "label": f"{label} {pct:+.2f}%" if pct is not None else label,
                "pct": pct, "value": d[0], "prev": d[1], "hit": hit,
            })
        else:
            observations.append({"label": f"{label}（無數據）", "pct": None, "value": None, "prev": None, "hit": False})

    # 5. S&P 500 own-day move (big-trend day itself)
    spx = _bs_day_change("^GSPC")
    if spx:
        pct = (spx[0] - spx[1]) / spx[1] * 100.0 if spx[1] else None
        hit = pct is not None and abs(pct) > BS_SPX_MOVE_PCT
        observations.append({
            "label": f"SPX {pct:+.2f}%" if pct is not None else "SPX",
            "pct": pct, "value": spx[0], "prev": spx[1], "hit": hit,
        })
    else:
        observations.append({"label": "SPX（無數據）", "pct": None, "value": None, "prev": None, "hit": False})

    hits = [o for o in observations if o.get("hit")]
    return hits, observations


def _bs_tick() -> None:
    """One detector cycle: fetch quotes → evaluate conditions → update state.

    Runs in a worker thread (network calls are blocking); never raises.
    On the first cycle that trips the threshold a Telegram alert is sent.
    """
    now_ts = time.monotonic()
    hits, observations = _bs_check_conditions()

    with _bs_lock:
        # Black swan only when >= BS_MIN_HITS of the 4 conditions fire together.
        black_swan = len(hits) >= BS_MIN_HITS
        first_hit = False
        if black_swan:
            first_hit = not _bs_state["blocked"]
            _bs_state["blocked"] = True
            _bs_state["last_hit_ts"] = now_ts
            # Tier escalates with hits; never de-escalates mid-episode
            # (only reset to "normal" at next first_hit or unblock).
            if first_hit:
                _bs_state["severity"] = "severe" if len(hits) >= BS_SEVERE_HITS else "normal"
            elif len(hits) >= BS_SEVERE_HITS:
                _bs_state["severity"] = "severe"
            if _bs_state["since_et"] is None:
                _bs_state["since_et"] = datetime.now(tz=_EASTERN)
            _bs_state["details"] = [dict(h) for h in hits]
        elif (
            _bs_state["blocked"]
            and _bs_state["last_hit_ts"] is not None
            and (now_ts - _bs_state["last_hit_ts"]) >= BLACK_SWAN_CALM
        ):
            # Fewer hits remains blocking until CALM passes, so a flickering
            # 2-of-4 situation does not flap the gate open every 5 minutes.
            _bs_state["blocked"] = False
            _bs_state["since_et"] = None
            _bs_state["details"] = []
            _bs_state["severity"] = "normal"
        elif _bs_state["blocked"]:
            # Still blocked (within calm), fewer hits: keep the current hits
            # visible so the UI shows the tail end of the episode.
            _bs_state["details"] = [dict(h) for h in hits]

        blocked_now = _bs_state["blocked"]
        _bs_state["last_check_et"] = datetime.now(tz=_EASTERN)
        _bs_state["hits_count"] = len(hits)
        _bs_state["observations"] = [dict(o) for o in observations]
        details = _bs_state["details"] if (first_hit and blocked_now) else []

    if details:                      # first engagement of this episode
        tier = ("🚨🚨 BLACK SWAN — SEVERE 🚨🚨" if _bs_state["severity"] == "severe"
                else "⚠️ BLACK SWAN ALERT ⚠️")
        lines = "\n".join(str(h["label"]) for h in details)
        _tg_send(
            f"{tier}\n{lines}\n建議今日不下單 (advisory — orders NOT blocked)"
        )


async def _bs_loop() -> None:
    """Background detector loop (async task started in lifespan).

    Runs exactly ONE check per trading day: sleeps until 09:29 ET
    (one minute before the 09:30 US cash open), evaluates the conditions,
    then holds any resulting block through the trading day until the
    16:00 ET close, where it is cleared.  The next check is 09:29 ET the
    following trading day.
    """
    while True:
        # 1. Sleep until the once-daily check time (09:29 ET by default).
        wait = (_bs_next_check_dt() - datetime.now(tz=_EASTERN)).total_seconds()
        await asyncio.sleep(max(0.0, wait))
        try:
            await asyncio.to_thread(_bs_tick)
        except Exception as exc:
            print(f"[blackswan] tick failed: {exc}")
        # 2. Sleep until the cash close (16:00 ET); if the check tripped,
        #    the block stays on for the whole trading day until then.
        wait = (_bs_next_close_dt() - datetime.now(tz=_EASTERN)).total_seconds()
        await asyncio.sleep(max(0.0, wait))
        with _bs_lock:
            if _bs_state["blocked"]:
                _bs_clear_block_locked()


@app.get("/api/blackswan")
async def blackswan_state():
    """Current black-swan block state for the UI (polled every few seconds)."""
    with _bs_lock:
        return {
            "blocked":       _bs_state["blocked"],
            "severity":      _bs_state["severity"],
            "min_hits":      BS_MIN_HITS,
            "severe_hits":   BS_SEVERE_HITS,
            "since_et":      _bs_state["since_et"].isoformat() if _bs_state["since_et"] else None,
            "last_check_et": _bs_state["last_check_et"].isoformat() if _bs_state["last_check_et"] else None,
            "hits_count":    _bs_state["hits_count"],
            "min_hits":      BS_MIN_HITS,
            "observations":  [dict(o) for o in _bs_state["observations"]],
            "thresholds": {
                "nq_gap_pct":     BS_NQ_GAP_PCT,
                "vix_level":      BS_VIX_LEVEL,
                "vix_move_pct":   BS_VIX_MOVE_PCT,
                "oil_move_pct":   BS_OIL_MOVE_PCT,
                "asia_move_pct":  BS_ASIA_MOVE_PCT,
                "spx_move_pct":   BS_SPX_MOVE_PCT,
                "check_time":     BS_CHECK_TIME,
                "close_time":     "16:00",
            },
            "details":       [dict(d) for d in _bs_state["details"]],
        }


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
    cancel_next_day_1pm: bool = True    # kept for API compatibility; entry TIF is always DAY
    # Options / Futures only
    expiry: Optional[str] = None        # YYYYMMDD
    strike: Optional[float] = None
    right: Optional[str] = None         # C | P


class ModifyRequest(BaseModel):
    lmt_price: Optional[float] = None
    quantity: Optional[float] = None


class CsvImportRequest(BaseModel):
    csv: str
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_contract(req: OrderRequest):
    st = req.sec_type.upper()
    # IB API class-share symbols use a space (BRK B), not a hyphen (BRK-B);
    # the hyphen form returns "No security definition found".
    sym = req.symbol.upper().replace("-", " ") if st == "STK" else req.symbol.upper()
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
    global is_paper_account
    try:
        if ib.isConnected():
            ib.disconnect()
        await ib.connectAsync(req.host, req.port, clientId=req.client_id)
        # Allow ib_insync's auto-subscriptions (reqAccountUpdates, reqPositions)
        # time to receive their first data batch from IB.
        await asyncio.sleep(2)
        # Detect paper account: IB paper account IDs start with "DU".
        managed = getattr(ib.client, "managedAccounts", "") or ""
        accts = [a for a in managed.split(",") if a]
        is_paper_account = any(a.startswith("DU") for a in accts)
        return {
            "status": "connected",
            "host": req.host,
            "port": req.port,
            "client_id": req.client_id,
            "is_paper": is_paper_account,
            "accounts": accts,
        }
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
                print('trade', t)
                print()
                continue
            result.append({
                "order_id":   o.orderId,
                "parent_id":  o.parentId,
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
# Finished / overnight trades
# ---------------------------------------------------------------------------
# A trade is reported here when it was *not* finished within the same trading
# day — i.e. the opening fill and the closing fill fall on different calendar
# days, OR the closing fill lands after the regular session close (16:00 ET)
# on the same day (after-hours / extended-hours close).

# EOD auto-close leg activates at 15:55 ET (see _next_trading_day_1555_et).
# A trade qualifies as "not finished within the day" when the close fill is on
# a later date than the open, OR at/after the EOD close time (15:55 ET) on the
# same date — i.e. closed by the bracket's EOD leg rather than intraday TP/SL.
_MARKET_CLOSE_ET = (15, 55)


def _aware_utc(dt: datetime) -> datetime:
    """IB execution times may arrive naive; assume UTC then we can convert."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _to_eastern(dt: datetime) -> datetime:
    return _aware_utc(dt).astimezone(_EASTERN)


def _parse_iso(iso: str) -> datetime:
    """Parse an ISO timestamp (possibly from JSON) into a tz-aware datetime."""
    t = datetime.fromisoformat(iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def _fifo_match(fills: list) -> list:
    """FIFO-match buys against sells per (symbol, secType, currency).

    `fills` is a list of plain dicts with keys: exec_id, time (ISO str with tz),
    symbol, sec_type, currency, side, shares, price.  Returns a list of closed
    lots (dicts with open_time/close_time in ET, open_price, close_price, etc.).
    """
    groups: dict = {}
    for f in fills:
        key = (f["symbol"], f["sec_type"], f["currency"] or "")
        groups.setdefault(key, []).append(f)

    result = []
    for (sym, sec, cur), fl in groups.items():
        fl.sort(key=lambda x: _parse_iso(x["time"]))
        long_q: deque = deque()    # each entry: [shares_left, open_time_et, open_price]
        short_q: deque = deque()
        for f in fl:
            side = (f["side"] or "").upper()
            # IB execution sides: BOT (bought), SLD (sold), SSHORT (short sell).
            # "BOT" → buy (opens long / closes short); anything else → sell.
            is_buy = side in ("BOT", "BUY")
            shares = float(f["shares"])
            close_et = _to_eastern(_parse_iso(f["time"]))
            close_price = float(f["price"])
            opposing = short_q if is_buy else long_q
            own_side = "SHORT" if is_buy else "LONG"
            while shares > 1e-9 and opposing:
                lot = opposing[0]
                lot_shares, lot_open_et, lot_open_price = lot[0], lot[1], lot[2]
                matched = min(shares, lot_shares)
                if is_buy:                     # closing a short
                    pnl = (lot_open_price - close_price) * matched
                else:                          # closing a long
                    pnl = (close_price - lot_open_price) * matched
                if (cur or "").upper() == "USD":
                    pnl *= 7.8  # USD→HKD conversion used by open-orders / positions views
                open_date = lot_open_et.date()
                close_date = close_et.date()
                qualifies = (
                    close_date > open_date or
                    (close_date == open_date and
                     (close_et.hour, close_et.minute) >= _MARKET_CLOSE_ET)
                )
                if qualifies:
                    result.append({
                        "symbol":      sym,
                        "sec_type":    sec,
                        "currency":    cur,
                        "side":        own_side,
                        "shares":      float(round(matched, 6)),
                        "open_time":   lot_open_et.isoformat(),
                        "close_time":  close_et.isoformat(),
                        "open_price":  lot_open_price,
                        "close_price": close_price,
                        "pnl":         float(round(pnl, 4)),
                        "exec_id":     f.get("exec_id", ""),
                    })
                lot[0] -= matched
                shares -= matched
                if lot[0] <= 1e-9:
                    opposing.popleft()
            if shares > 1e-9:
                target = long_q if is_buy else short_q
                target.append([shares, close_et, close_price])
    result.sort(key=lambda r: r["close_time"], reverse=True)
    return result, groups


@app.get("/api/overnight-trades")
async def overnight_trades(days: int = 60):
    """Finished trades that were held past the same-day session close.

    IB's reqExecutions() only returns the current day's fills (per IB's official
    docs: "Only the current day's executions can be retrieved").  Opening fills
    from previous days are therefore loaded from the local JSONL log kept by
    the running server (every live fill is appended to fills.jsonl).  We merge
    the on-disk history with today's reqExecutions() result and FIFO-match
    buys against sells per (symbol, secType, currency).

    A closed lot qualifies as "overnight" when the close fill is on a later
    date than the open, OR at/after 15:55 ET on the same date (the EOD close
    leg of the bracket fires at 15:55 ET).
    """
    if not ib.isConnected():
        raise HTTPException(400, "Not connected to IB")
    try:
        from ib_insync import ExecutionFilter

        # 1. Local on-disk fills (preserves yesterday's opens across restarts).
        local_fills = _load_local_fills(days=days)

        # 2. Today's fills from IB (the only fills the API will return).
        today_raw = await ib.reqExecutionsAsync(ExecutionFilter())
        today_dtos = []
        for f in list(today_raw) + list(ib.fills()):
            ex = f.execution
            c = f.contract
            today_dtos.append({
                "exec_id":  ex.execId,
                "time":     _to_eastern(ex.time).isoformat(),
                "symbol":   c.symbol,
                "sec_type": c.secType,
                "currency": c.currency or "",
                "side":     ex.side or "",
                "shares":   float(ex.shares),
                "price":    float(ex.price),
            })

        # 3. Merge by execId (preferring today's authoritative version).
        seen = set()
        merged = []
        for d in today_dtos + local_fills:
            eid = d.get("exec_id") or ""
            if eid and eid in seen:
                continue
            if eid:
                seen.add(eid)
            merged.append(d)

        # Also record today's fills to disk so they survive a restart later.
        for f in list(today_raw) + list(ib.fills()):
            _record_fill(f)

        cutoff = datetime.now(tz=_EASTERN) - timedelta(days=max(days or 0, 1))
        merged = [d for d in merged
                  if _to_eastern(_parse_iso(d["time"])) >= cutoff]

        result, groups = _fifo_match(merged)
        debug = (
            f"{len(local_fills)} local + {len(today_dtos)} today "
            f"= {len(merged)} fills, {len(groups)} groups, {len(result)} overnight"
        )
        return {"ok": True, "trades": result, "debug": debug}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/overnight-trades/debug")
async def overnight_trades_debug(days: int = 60):
    """Dump raw fills (from local log + today's reqExecutions) for inspection."""
    if not ib.isConnected():
        raise HTTPException(400, "Not connected to IB")
    try:
        from ib_insync import ExecutionFilter
        local_fills = _load_local_fills(days=days)
        today_raw = await ib.reqExecutionsAsync(ExecutionFilter())
        today_dtos = []
        for f in list(today_raw) + list(ib.fills()):
            ex = f.execution
            c = f.contract
            today_dtos.append({
                "exec_id":  ex.execId,
                "time":     _to_eastern(ex.time).isoformat(),
                "symbol":   c.symbol,
                "sec_type": c.secType,
                "currency": c.currency or "",
                "side":     ex.side or "",
                "shares":   float(ex.shares),
                "price":    float(ex.price),
                "order_id": ex.orderId,
            })
        seen = set()
        merged = []
        for d in today_dtos + local_fills:
            eid = d.get("exec_id") or ""
            if eid and eid in seen:
                continue
            if eid:
                seen.add(eid)
            merged.append(d)
        return {
            "ok": True,
            "fills": merged,
            "count": len(merged),
            "local_count": len(local_fills),
            "today_count": len(today_dtos),
            "file": _FILLS_FILE,
        }
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ---------------------------------------------------------------------------
# Order management endpoints
# ---------------------------------------------------------------------------

@app.post("/api/orders")
async def place_order(req: OrderRequest):
    if not ib.isConnected():
        raise HTTPException(400, "Not connected to IB")

    # Earnings-day (業績日) gate — block BEFORE-today / AFTER-prev-trading-day.
    # (Black-swan is advisory only — see _bs_loop / _bs_tick — NOT gated here.)
    earn_map = await _earnings_block_async()
    e = earn_map.get(_norm_earn_symbol(req.symbol))
    if not e:
        e = await asyncio.to_thread(_yf_earn_block, req.symbol)
    if e:
        when = "before market open" if e["pub_type"] == "BEFORE" else "after market close"
        raise HTTPException(
            400,
            f"Order blocked for {req.symbol}: earnings announced {when} "
            f"({e['earnings_date']}) | {req.symbol} 業績日，禁止下單",
        )

    try:
        contract = _build_contract(req)
        ot = req.order_type.lower()

        if ot == "bracket":
            if not all([req.entry_price is not None, req.take_profit is not None, req.stop_loss is not None]):
                raise HTTPException(400, "entry_price, take_profit, stop_loss required for bracket orders")

            action   = req.action.upper()
            rev_action = "SELL" if action == "BUY" else "BUY"
            qty      = req.quantity

            # We build all four legs manually so TP, SL, and the close-at-EOD
            # market order can share one OCA group (ocaType=1).  IB does NOT
            # allow MOC orders in an OCA group, so the close leg is a regular
            # MARKET order with GoodAfterTime set to 15:55 ET — it stays
            # dormant until near market close, then executes at market.
            #
            # When *any* of TP / SL / Close fills, IB cancels the other two
            # automatically.  The entry (parent) is linked via parentId so TP,
            # SL, and Close only become active after the entry fills.

            parent_id = ib.client.getReqId()
            tp_id     = ib.client.getReqId()
            sl_id     = ib.client.getReqId()
            close_id  = ib.client.getReqId()

            oca_group = f"bracket_{parent_id}"

            # 1. Entry (parent) — LMT, GTD 12:30 ET, transmit=False
            entry = LimitOrder(action, qty, req.entry_price)
            entry.orderId        = parent_id
            entry.tif            = "GTD"
            entry.goodTillDate   = _next_day_1pm_eastern()
            entry.transmit       = False

            # 2. Take-profit (child) — LMT, DAY, OCA, transmit=False
            tp = LimitOrder(rev_action, qty, req.take_profit)
            tp.orderId    = tp_id
            tp.parentId   = parent_id
            tp.tif        = "DAY"
            tp.ocaGroup   = oca_group
            tp.ocaType    = 1
            tp.transmit   = False

            # 3. Stop-loss (child) — STP, DAY, OCA, transmit=False
            sl = StopOrder(rev_action, qty, req.stop_loss)
            sl.orderId    = sl_id
            sl.parentId   = parent_id
            sl.tif        = "DAY"
            sl.ocaGroup   = oca_group
            sl.ocaType    = 1
            sl.transmit   = False

            # 4. Close at EOD (child) — MARKET, DAY, GoodAfterTime 15:55 ET,
            #    OCA, transmit=True  (this sends all four orders to IB)
            close = MarketOrder(rev_action, qty)
            close.orderId        = close_id
            close.parentId       = parent_id
            close.tif            = "DAY"
            close.goodAfterTime  = _next_trading_day_1555_et()
            close.ocaGroup       = oca_group
            close.ocaType        = 1
            close.transmit       = True

            # Place all four legs — only the last (close) has transmit=True,
            # so IB receives them atomically as one bracket + OCA group.
            trades = [ib.placeOrder(contract, o) for o in (entry, tp, sl, close)]
            await asyncio.sleep(1)

            return [
                {
                    "order_id":   t.order.orderId,
                    "status":     t.orderStatus.status,
                    "order_type": t.order.orderType,
                    "oca_group":  t.order.ocaGroup,
                }
                for t in trades
            ]

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


# ---------------------------------------------------------------------------
# CSV import (export-file -> bracket orders)
# ---------------------------------------------------------------------------
# Supports strategy-export files shaped like:
#     ticker,side,action,shares,entry,tp,sl,notional_usd,weight,score,...
#     BRK-B,-1,做空,15,512.77,509.20,518.63,...
# `action` 做多/long/buy → BUY, 做空/short/sell → SELL  (or `side`: 1=BUY, -1=SELL).
# Extra columns are ignored.  Each valid row becomes a bracket order.

_CSV_ALIASES = {
    "ticker": "ticker", "symbol": "ticker",
    "action": "action", "side": "side",
    "shares": "shares", "qty": "shares", "quantity": "shares",
    "entry": "entry", "entry_price": "entry",
    "tp": "tp", "take_profit": "tp",
    "sl": "sl", "stop_loss": "sl",
    "notional_usd": "notional_usd", "weight": "weight", "score": "score",
}


def _csv_action(action: str, side: str) -> Optional[str]:
    a = (action or "").strip().lower()
    s = (side or "").strip().lower()

    def norm(v: str) -> Optional[str]:
        if v in ("做多", "long", "buy", "b"):
            return "BUY"
        if v in ("做空", "short", "sell", "s"):
            return "SELL"
        if v in ("1", "+1"):
            return "BUY"
        if v == "-1":
            return "SELL"
        return None

    return norm(a) or norm(s)


def _parse_orders_csv(text: str):
    """Parse strategy-export CSV text into bracket-order rows.

    Returns (orders, errors).  `orders` is a list of dicts with keys
    symbol / action / shares / entry / tp / sl / notional_usd; `errors` is a
    list of "line N: message" strings for skipped rows.
    """
    import csv as _csv
    import io as _io

    errors: list = []
    orders: list = []

    text = text.lstrip("\ufeff")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return [], ["File is empty"]

    # Light delimiter sniff (comma by default; tab/semicolon supported).
    header_line = lines[0]
    delim = ","
    if header_line.count("\t") > header_line.count(","):
        delim = "\t"
    elif header_line.count(";") > header_line.count(","):
        delim = ";"

    reader = _csv.reader(_io.StringIO("\n".join(lines)), delimiter=delim)
    try:
        header = next(reader)
    except StopIteration:
        return [], ["Missing header row"]

    colmap: dict = {}   # column index -> canonical name
    for i, raw in enumerate(header):
        canon = _CSV_ALIASES.get((raw or "").strip().lstrip("\ufeff").lower())
        if canon and canon not in colmap:
            colmap[i] = canon

    required = {"ticker", "shares", "entry", "tp", "sl"}
    missing = required - set(colmap.values())
    if missing:
        return [], ["Missing required columns: " + ", ".join(sorted(missing)) +
                    (" (need action or side)" if "action" not in colmap.values() and
                     "side" not in colmap.values() else "")]

    def get(row, name):
        for i, canon in colmap.items():
            if canon == name and i < len(row):
                return (row[i] or "").strip()
        return ""

    def num(v):
        v = (v or "").replace(",", "").strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    for lineno, row in enumerate(reader, start=2):
        if not row or all(not c.strip() for c in row):
            continue
        ticker = get(row, "ticker").upper()
        if not ticker:
            errors.append(f"line {lineno}: missing ticker")
            continue
        action = _csv_action(get(row, "action"), get(row, "side"))
        if not action:
            errors.append(f"line {lineno}: {ticker} — unknown action/side "
                          f"'{get(row, 'action')}' / '{get(row, 'side')}'")
            continue
        shares = num(get(row, "shares"))
        entry  = num(get(row, "entry"))
        tp     = num(get(row, "tp"))
        sl     = num(get(row, "sl"))
        if shares is None or shares <= 0:
            errors.append(f"line {lineno}: {ticker} — invalid shares '{get(row, 'shares')}'")
            continue
        for nm, v in (("entry", entry), ("tp", tp), ("sl", sl)):
            if v is None or v <= 0:
                errors.append(f"line {lineno}: {ticker} — invalid {nm} price")
                break
        else:
            # Direction sanity: short (SELL) → TP below entry, SL above;
            # long (BUY) → the mirror.  A reversed bracket would fire instantly.
            ok = (tp < entry < sl) if action == "SELL" else (sl < entry < tp)
            if not ok:
                errors.append(f"line {lineno}: {ticker} — TP/SL on wrong side of "
                              f"entry for {action} (entry={entry}, tp={tp}, sl={sl})")
                continue
            orders.append({
                "symbol":        ticker,
                "action":        action,
                "shares":        int(shares) if float(shares).is_integer() else shares,
                "entry":         round(entry, 2),
                "tp":            round(tp, 2),
                "sl":            round(sl, 2),
                "notional_usd":  num(get(row, "notional_usd")),
            })
    return orders, errors


@app.post("/api/orders/csv")
async def import_orders_csv(req: CsvImportRequest):
    """Import a CSV export file and place one bracket order per valid row.

    Set dry_run=true to parse/validate only (used by the UI preview).
    """
    orders, errors = _parse_orders_csv(req.csv)
    if req.dry_run:
        return {"ok": True, "dry_run": True, "orders": orders, "errors": errors}
    if not ib.isConnected():
        raise HTTPException(400, "Not connected to IB")
    results = []
    for r in orders:
        try:
            order_req = OrderRequest(
                symbol=r["symbol"], sec_type="STK", exchange="SMART", currency="USD",
                action=r["action"], quantity=r["shares"], order_type="bracket",
                entry_price=r["entry"], take_profit=r["tp"], stop_loss=r["sl"],
            )
            res = await place_order(order_req)
            results.append({"symbol": r["symbol"], "ok": True, "orders": res})
        except HTTPException as exc:
            results.append({"symbol": r["symbol"], "ok": False, "error": exc.detail})
        except Exception as exc:
            results.append({"symbol": r["symbol"], "ok": False, "error": str(exc)})
    placed = sum(1 for x in results if x["ok"])
    return {"ok": True, "dry_run": False, "count": len(orders),
            "placed": placed, "results": results, "errors": errors}


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

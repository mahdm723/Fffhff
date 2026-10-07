"""V6 phase 2: the market list (top gainers / losers of the last 24 h) from Bybit's public spot API.

Only the server talks to Bybit (no account, no key: public market data). Every MARKET_REFRESH_SECONDS one
instance fetches all spot tickers, keeps USDT pairs with real volume (no stablecoins, no leveraged tokens,
nothing in MARKET_EXCLUDE), ranks them by 24 h change and stores the top MARKET_TOP_N each way in
`market_snapshots`, plus a 24 h hourly sparkline for those coins only. Phones read /api/market.

When Bybit cannot be reached the last good list stays and is marked `stale`. Several app instances share
the row: an instance that finds a fresh enough row just reads it instead of calling Bybit again.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.models import MarketSnapshot

log = logging.getLogger("dzplay.market")
DOMAINS = ("https://api.bybit.com", "https://api.bytick.com")
_KEY = "latest"
_LEVERAGED = re.compile(r"(\d+[LS]|UP|DOWN|BULL|BEAR)USDT$")
_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}USDT$")
_STALE_FACTOR = 3  # older than 3 refresh periods = stale


class MarketError(Exception):
    """Bybit unreachable or answered something unusable (message is safe to log/show to the admin)."""


@dataclass
class _Memory:
    snapshot: dict | None = None
    sparks: dict[str, list[float]] = field(default_factory=dict)
    sparks_at: float = 0.0
    last_error: str | None = None


_mem = _Memory()


def reset_memory() -> None:  # tests
    global _mem
    _mem = _Memory()


def _csv(value: str) -> set[str]:
    return {v.strip().upper() for v in (value or "").split(",") if v.strip()}


def _num(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _client(settings: Settings, transport=None) -> httpx.Client:
    return httpx.Client(timeout=settings.MARKET_TIMEOUT, transport=transport,
                        headers={"Accept": "application/json", "User-Agent": "dzplay-market/1"})


def _get(client: httpx.Client, url: str, params: dict) -> list:
    try:
        r = client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise MarketError("timeout") from exc
    except httpx.HTTPError as exc:
        raise MarketError(f"network error: {type(exc).__name__}") from exc
    if r.status_code == 403:
        raise MarketError("HTTP 403 (blocked from this server's country/IP — try the other domain)")
    if r.status_code != 200:
        raise MarketError(f"HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError as exc:
        raise MarketError("not JSON") from exc
    if not isinstance(body, dict) or body.get("retCode") != 0:
        raise MarketError(f"Bybit error {body.get('retCode') if isinstance(body, dict) else '?'}")
    items = (body.get("result") or {}).get("list")
    if not isinstance(items, list):
        raise MarketError("unexpected answer")
    return items


def fetch_tickers(settings: Settings, base_url: str | None = None, transport=None) -> list[dict]:
    with _client(settings, transport) as client:
        return _get(client, (base_url or settings.MARKET_BASE_URL).rstrip("/") + "/v5/market/tickers",
                    {"category": "spot"})


def select(settings: Settings, tickers: list[dict]) -> tuple[list[dict], list[dict], int]:
    """(gainers, losers, how many pairs qualified) from raw Bybit tickers."""
    stable, excluded = _csv(settings.MARKET_STABLECOINS), _csv(settings.MARKET_EXCLUDE)
    rows = []
    for t in tickers:
        if not isinstance(t, dict):
            continue
        symbol = str(t.get("symbol") or "").upper()
        if not _SYMBOL.match(symbol) or symbol in excluded or _LEVERAGED.search(symbol):
            continue
        base = symbol[:-4]
        if base in stable or base == "USDT":
            continue
        price, pct = _num(t.get("lastPrice")), _num(t.get("price24hPcnt"))
        turnover = _num(t.get("turnover24h")) or 0.0
        if not price or price <= 0 or pct is None or turnover < settings.MARKET_MIN_TURNOVER_24H:
            continue
        rows.append({"symbol": symbol, "base": base, "price": t.get("lastPrice"), "change_pct": round(pct * 100, 2),
                     "high24": t.get("highPrice24h"), "low24": t.get("lowPrice24h"), "turnover24": round(turnover)})
    n = settings.MARKET_TOP_N
    up = sorted((r for r in rows if r["change_pct"] > 0), key=lambda r: (-r["change_pct"], -r["turnover24"]))[:n]
    down = sorted((r for r in rows if r["change_pct"] < 0), key=lambda r: (r["change_pct"], -r["turnover24"]))[:n]
    return up, down, len(rows)


def fetch_spark(client: httpx.Client, base_url: str, symbol: str) -> list[float]:
    """24 hourly closes, oldest first (Bybit returns newest first)."""
    items = _get(client, base_url.rstrip("/") + "/v5/market/kline",
                 {"category": "spot", "symbol": symbol, "interval": "60", "limit": "24"})
    closes = [_num(k[4]) for k in reversed(items) if isinstance(k, list) and len(k) >= 5]
    return [c for c in closes if c is not None]


def _load(db: Session) -> MarketSnapshot | None:
    return db.get(MarketSnapshot, _KEY)


def refresh(db: Session, settings: Settings, transport=None, force: bool = False) -> dict | None:
    """One refresh round (the background loop). Returns the snapshot in use (new or previous)."""
    if not settings.MARKET_ENABLED:
        return None
    now = clock.utcnow()
    row = _load(db)
    fresh_for = timedelta(seconds=max(10, settings.MARKET_REFRESH_SECONDS * 0.8))
    if row is not None and not force and now - row.checked_at < fresh_for:
        _mem.snapshot = _row_dict(row)  # another instance refreshed it a moment ago
        return _mem.snapshot
    base = settings.MARKET_BASE_URL
    try:
        up, down, count = select(settings, fetch_tickers(settings, base, transport))
        if not up and not down:
            raise MarketError("no pair qualified (check MARKET_MIN_TURNOVER_24H)")
        coins = [r["symbol"] for r in up + down]
        if (time.monotonic() - _mem.sparks_at > settings.MARKET_KLINE_REFRESH_SECONDS
                or any(s not in _mem.sparks for s in coins)):
            with _client(settings, transport) as client:
                sparks = {}
                for s in coins:
                    try:
                        sparks[s] = fetch_spark(client, base, s)
                    except MarketError:
                        sparks[s] = _mem.sparks.get(s, [])
            _mem.sparks, _mem.sparks_at = sparks, time.monotonic()
        for r in up + down:
            r["spark"] = _mem.sparks.get(r["symbol"], [])
        data = {"gainers": up, "losers": down, "pairs": count}
        if row is None:
            row = MarketSnapshot(key=_KEY, data="", source=base, updated_at=now, checked_at=now)
            db.add(row)
        row.data, row.source, row.updated_at, row.checked_at, row.last_error = json.dumps(data), base, now, now, None
        _mem.last_error = None
    except MarketError as exc:
        log.warning("market refresh failed (%s): %s", base, exc)
        _mem.last_error = str(exc)[:200]
        if row is not None:
            row.checked_at, row.last_error = now, _mem.last_error
    db.commit()
    _mem.snapshot = _row_dict(row) if row is not None else None
    return _mem.snapshot


def _row_dict(row: MarketSnapshot) -> dict:
    data = json.loads(row.data)
    return {**data, "updated_at": row.updated_at, "checked_at": row.checked_at, "source": row.source,
            "last_error": row.last_error}


def current(db: Session, settings: Settings) -> dict:
    """What /api/market shows."""
    snap = _mem.snapshot
    if snap is None:
        row = _load(db)
        snap = _mem.snapshot = _row_dict(row) if row is not None else None
    out = {"enabled": settings.MARKET_ENABLED, "gainers": [], "losers": [], "updated_at": None, "stale": True,
           "disclaimer": settings.MARKET_DISCLAIMER, "refresh_seconds": settings.MARKET_REFRESH_SECONDS}
    if not settings.MARKET_ENABLED or snap is None:
        return out
    age = (clock.utcnow() - snap["updated_at"]).total_seconds()
    out.update(gainers=snap["gainers"], losers=snap["losers"], updated_at=_iso(snap["updated_at"]),
               stale=age > settings.MARKET_REFRESH_SECONDS * _STALE_FACTOR)
    return out


def status(db: Session, settings: Settings) -> dict:
    """Admin card: last update, source, health."""
    row = _load(db)
    snap = _row_dict(row) if row is not None else None
    return {"enabled": settings.MARKET_ENABLED, "source": settings.MARKET_BASE_URL,
            "updated_at": _iso(snap["updated_at"]) if snap else None,
            "checked_at": _iso(snap["checked_at"]) if snap else None,
            "last_error": (snap or {}).get("last_error") or _mem.last_error,
            "pairs": (snap or {}).get("pairs"), "stale": current(db, settings)["stale"]}


def check(settings: Settings, transport=None) -> list[dict]:
    """`admin_cli market-check`: try both domains from this server."""
    out = []
    for base in DOMAINS:
        started = time.monotonic()
        try:
            tickers = fetch_tickers(settings, base, transport)
            up, down, count = select(settings, tickers)
            out.append({"domain": base, "ok": True, "ms": round((time.monotonic() - started) * 1000),
                        "tickers": len(tickers), "pairs": count,
                        "top_gainer": f"{up[0]['symbol']} {up[0]['change_pct']:+.2f}%" if up else None,
                        "top_loser": f"{down[0]['symbol']} {down[0]['change_pct']:+.2f}%" if down else None})
        except MarketError as exc:
            out.append({"domain": base, "ok": False, "ms": round((time.monotonic() - started) * 1000), "error": str(exc)})
    return out


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="milliseconds") + "Z" if dt else None

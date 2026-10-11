"""V6 phase 2: the market (top gainers/losers from Bybit's public spot API, served by /api/market)."""

from __future__ import annotations

import json

import httpx
import pytest

from app import clock
from app.services import market

ORIGIN = "https://api.bybit.com"


def _t(symbol, pct, turnover=5_000_000, price="1.5", **kw):
    return {"symbol": symbol, "lastPrice": price, "price24hPcnt": str(pct), "turnover24h": str(turnover),
            "highPrice24h": "2", "lowPrice24h": "1", **kw}


TICKERS = [
    _t("BTCUSDT", 0.012, 900_000_000, "64000.5"),
    _t("ETHUSDT", -0.031, 400_000_000, "3100.2"),
    _t("SOLUSDT", 0.084),
    _t("DOGEUSDT", -0.12),
    _t("PEPEUSDT", 0.25, price="0.0000123"),
    _t("XRPUSDT", -0.05),
    _t("ADAUSDT", 0.02),
    _t("LINKUSDT", 0.03),
    _t("AVAXUSDT", 0.04),
    _t("DOTUSDT", 0.05),
    _t("NEARUSDT", 0.06),
    _t("TONUSDT", -0.07),
    # never shown
    _t("USDCUSDT", 0.9),  # stablecoin
    _t("FDUSDUSDT", -0.9),  # stablecoin
    _t("BTC3LUSDT", 0.5),  # leveraged token
    _t("ETH3SUSDT", -0.5),
    _t("XRPUPUSDT", 0.5),
    _t("ADADOWNUSDT", -0.5),
    _t("THINUSDT", 0.7, turnover=1000),  # too little volume
    _t("BTCEUR", 0.6),  # not a USDT pair
    _t("ETHBTC", 0.6),
    _t("BADUSDT", "x"),  # garbage
    _t("ZEROUSDT", 0.3, price="0"),
    {"symbol": None},
    "junk",
]


def _kline(symbol):
    # newest first, as Bybit returns it: closes 24..1
    return [[str(1700000000000 + i), "1", "2", "0.5", str(24 - i), "10", "10"] for i in range(24)]


class FakeBybit:
    def __init__(self, tickers=None):
        self.tickers = TICKERS if tickers is None else tickers
        self.down: set[str] = set()  # hosts that fail
        self.status = 200
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.url.host}{request.url.path}")
        if request.url.host in self.down:
            raise httpx.ConnectError("unreachable", request=request)
        if self.status != 200:
            return httpx.Response(self.status, text="forbidden")
        if request.url.path == "/v5/market/tickers":
            assert request.url.params["category"] == "spot"
            return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"category": "spot", "list": self.tickers}})
        if request.url.path == "/v5/market/kline":
            assert request.url.params["interval"] == "60" and request.url.params["limit"] == "24"
            return httpx.Response(200, json={"retCode": 0, "result": {"list": _kline(request.url.params["symbol"])}})
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def _fresh_memory():
    market.reset_memory()
    yield
    market.reset_memory()


@pytest.fixture
def mk(hx):
    hx.settings.MARKET_REFRESH_SECONDS = 60
    fake = FakeBybit()
    transport = httpx.MockTransport(fake)

    def refresh(force=True):
        with hx.db() as db:
            return market.refresh(db, hx.settings, transport=transport, force=force)

    return fake, refresh


def test_filters_and_ranking(hx):
    up, down, count = market.select(hx.settings, TICKERS)
    assert [r["symbol"] for r in up] == ["PEPEUSDT", "SOLUSDT", "NEARUSDT", "DOTUSDT", "AVAXUSDT", "LINKUSDT"]
    assert [r["symbol"] for r in down] == ["DOGEUSDT", "TONUSDT", "XRPUSDT", "ETHUSDT"]
    assert count == 12  # only the real USDT pairs with volume
    pepe = up[0]
    assert pepe == {"symbol": "PEPEUSDT", "base": "PEPE", "price": "0.0000123", "change_pct": 25.0, "high24": "2",
                    "low24": "1", "turnover24": 5_000_000}
    hx.settings.MARKET_EXCLUDE = "pepeusdt, DOGEUSDT"
    hx.settings.MARKET_TOP_N = 2
    up, down, _ = market.select(hx.settings, TICKERS)
    assert [r["symbol"] for r in up] == ["SOLUSDT", "NEARUSDT"] and [r["symbol"] for r in down] == ["TONUSDT", "XRPUSDT"]
    hx.settings.MARKET_MIN_TURNOVER_24H = 500_000_000
    up, down, count = market.select(hx.settings, TICKERS)
    assert count == 1 and [r["symbol"] for r in up] == ["BTCUSDT"] and down == []


def test_api_shows_the_list_with_sparklines(hx, mk):
    fake, refresh = mk
    u = hx.user()
    empty = u.get("/api/market").json()
    assert empty["gainers"] == [] and empty["stale"] is True and empty["updated_at"] is None
    refresh()
    d = u.get("/api/market").json()
    assert d["enabled"] and d["stale"] is False and d["updated_at"].endswith("Z")
    assert len(d["gainers"]) == 6 and len(d["losers"]) == 4
    assert d["gainers"][0]["spark"] == [float(i) for i in range(1, 25)]  # oldest first
    assert "نصيحة" in d["disclaimer"] and d["refresh_seconds"] == 60
    # sparklines only for the coins shown, and not again on the next minute
    klines = [c for c in fake.calls if c.endswith("/kline")]
    assert len(klines) == 10
    refresh()
    assert len([c for c in fake.calls if c.endswith("/kline")]) == 10
    # needs a session (no anonymous scraping through us)
    assert hx.client().get("/api/market").status_code == 401


def test_bybit_down_keeps_last_list_marked_stale(hx, mk):
    fake, refresh = mk
    u = hx.user()
    refresh()
    good = u.get("/api/market").json()
    fake.down.add("api.bybit.com")
    clock.advance(61)
    refresh()
    d = u.get("/api/market").json()
    assert d["gainers"] == good["gainers"] and d["updated_at"] == good["updated_at"]
    assert d["stale"] is False  # 61 s: still within 3 refresh periods
    clock.advance(200)
    refresh()
    d = u.get("/api/market").json()
    assert d["stale"] is True and d["gainers"] == good["gainers"]
    with hx.db() as db:
        st = market.status(db, hx.settings)
    assert st["stale"] is True and "network error" in st["last_error"]
    # 403 (blocked country) is explained
    fake.down.clear()
    fake.status = 403
    refresh()
    with hx.db() as db:
        assert "403" in market.status(db, hx.settings)["last_error"]
    # recovers
    fake.status = 200
    refresh()
    d = u.get("/api/market").json()
    assert d["stale"] is False
    with hx.db() as db:
        assert market.status(db, hx.settings)["last_error"] is None


def test_switching_domain_from_the_panel(hx, mk):
    fake, refresh = mk
    adm = hx.admin()
    r = adm.put("/api/admin/settings", json={"changes": {"MARKET_BASE_URL": "https://api.bytick.com"}})
    assert r.status_code == 200, r.text
    assert hx.settings.MARKET_BASE_URL == "https://api.bytick.com"
    refresh()
    assert fake.calls and all(c.startswith("api.bytick.com") for c in fake.calls)
    # only the two Bybit domains can be chosen (no arbitrary host for the server to call)
    for bad in ("https://evil.example", "http://127.0.0.1:6379", "https://api.bybit.com.evil.io"):
        r = adm.put("/api/admin/settings", json={"changes": {"MARKET_BASE_URL": bad}})
        assert r.status_code == 400
    s = next(x for x in adm.get("/api/admin/settings").json()["settings"] if x["key"] == "MARKET_BASE_URL")
    assert s["type"] == "choice" and s["choices"] == ["https://api.bybit.com", "https://api.bytick.com"]


def test_disabled_from_the_panel(hx, mk):
    fake, refresh = mk
    u = hx.user()
    refresh()
    assert hx.admin().put("/api/admin/settings", json={"changes": {"MARKET_ENABLED": False}}).status_code == 200
    d = u.get("/api/market").json()
    assert d["enabled"] is False and d["gainers"] == []
    fake.calls.clear()
    assert refresh() is None and fake.calls == []


def test_other_instance_reuses_a_fresh_row(hx, mk):
    fake, refresh = mk
    refresh()
    n = len(fake.calls)
    market.reset_memory()  # another app instance
    snap = refresh(force=False)
    assert len(fake.calls) == n and len(snap["gainers"]) == 6
    clock.advance(60)
    refresh(force=False)
    assert len(fake.calls) > n


def test_admin_status_and_refresh(hx, mk):
    fake, _refresh = mk
    adm = hx.admin()
    st = adm.get("/api/admin/market").json()
    assert st["updated_at"] is None and st["source"] == ORIGIN
    # the panel button refreshes through the real (patched) transport
    orig = market._client
    market._client = lambda settings, transport=None: orig(settings, httpx.MockTransport(fake))
    try:
        st = adm.post("/api/admin/market/refresh").json()
    finally:
        market._client = orig
    assert st["updated_at"] and st["pairs"] == 12 and st["stale"] is False
    log = adm.get("/api/admin/audit").json()
    assert any(e["action"] == "market_refresh" for e in log["entries"])
    assert hx.user().get("/api/admin/market").status_code in (401, 403, 404)


def test_market_check_cli(hx, monkeypatch, capsys):
    from app import admin_cli
    from app.config import get_settings

    fake = FakeBybit()
    fake.down.add("api.bybit.com")
    monkeypatch.setenv("DATABASE_URL", hx.settings.DATABASE_URL)
    get_settings.cache_clear()
    try:
        admin_cli.main(["market-check"], market_transport=httpx.MockTransport(fake))
        out = capsys.readouterr().out
        assert "FAIL  https://api.bybit.com" in out and "network error" in out
        assert "OK    https://api.bytick.com" in out and "top gainer: PEPEUSDT +25.00%" in out
        assert "Switch the source to https://api.bytick.com" in out
        fake.down.add("api.bytick.com")
        with pytest.raises(SystemExit, match="not reachable"):
            admin_cli.main(["market-check"], market_transport=httpx.MockTransport(fake))
    finally:
        get_settings.cache_clear()


def test_snapshot_survives_restart(hx, mk):
    _fake, refresh = mk
    refresh()
    market.reset_memory()
    with hx.db() as db:
        d = market.current(db, hx.settings)
    assert len(d["gainers"]) == 6
    with hx.db() as db:
        from app.models import MarketSnapshot

        row = db.get(MarketSnapshot, "latest")
        assert json.loads(row.data)["pairs"] == 12

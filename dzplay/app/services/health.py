"""V6 phase 21: the state of the server for the panel («صحة النظام») and the Telegram alerts.

* report(): the request timings (services/metrics.py) + the machine (load, memory, disk) + the database
  (connections, size) + Redis (memory, clients) + this process's connection pool.
* problems(): what deploy/monitor.sh alerts about (admin_cli health-check), over the last 5 minutes:
  p95 above HEALTH_P95_MS, errors above HEALTH_ERROR_PCT (only with HEALTH_MIN_REQUESTS requests or more).
Nothing personal: endpoints are route templates, never a URL, an address or a user.
"""

from __future__ import annotations

import os
import shutil

from sqlalchemy import text

from app.config import Settings


def _meminfo() -> dict:
    out = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    out[key] = int(rest.split()[0]) * 1024
    except OSError:
        pass
    return out


def system(settings: Settings) -> dict:
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0
    mem = _meminfo()
    path = settings.MEDIA_CACHE_DIR if os.path.isdir(settings.MEDIA_CACHE_DIR) else "/"
    disk = shutil.disk_usage(path)
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    return {"cpus": os.cpu_count() or 1, "load1": round(load1, 2), "load5": round(load5, 2), "load15": round(load15, 2),
            "mem_total": total, "mem_used_pct": round(100 * (total - avail) / total, 1) if total else None,
            "disk_total": disk.total, "disk_used_pct": round(100 * disk.used / disk.total, 1) if disk.total else None}


def database(state) -> dict:
    engine = state.database.engine
    out: dict = {"dialect": engine.dialect.name}
    pool = engine.pool
    for name in ("size", "checkedout", "overflow"):
        fn = getattr(pool, name, None)
        if callable(fn):
            out[f"pool_{name}"] = fn()
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            out["connections"] = conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")).scalar()
            out["active"] = conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'active'")).scalar()
            out["max_connections"] = int(conn.execute(text("SHOW max_connections")).scalar())
            out["size_bytes"] = conn.execute(text("SELECT pg_database_size(current_database())")).scalar()
    return out


def redis_info(settings: Settings) -> dict | None:
    if not settings.REDIS_URL:
        return None
    import redis

    try:
        info = redis.Redis.from_url(settings.REDIS_URL, socket_timeout=2).info()
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__}
    return {"used_memory": info.get("used_memory"), "clients": info.get("connected_clients"),
            "ops_per_sec": info.get("instantaneous_ops_per_sec")}


def report(state, minutes: int = 15) -> dict:
    s = state.settings
    return {"metrics": state.metrics.snapshot(minutes), "system": system(s), "database": database(state),
            "redis": redis_info(s),
            "thresholds": {"p95_ms": s.HEALTH_P95_MS, "error_pct": s.HEALTH_ERROR_PCT, "min_requests": s.HEALTH_MIN_REQUESTS}}


def problems(settings: Settings, snapshot: dict) -> list[tuple[str, str]]:
    """(key, message) for each current problem — the format of deploy/monitor.sh."""
    t = snapshot["totals"]
    if t["count"] < settings.HEALTH_MIN_REQUESTS:
        return []
    out = []
    if t["p95_ms"] > settings.HEALTH_P95_MS:
        worst = ", ".join(f"{e['endpoint']} ({e['p95_ms']:.0f} ms)" for e in snapshot["slowest"][:3])
        out.append(("app:latency", f"التطبيق بطيء: p95 = {t['p95_ms']:.0f} ms خلال {snapshot['minutes']} دقائق "
                                   f"(الحد {settings.HEALTH_P95_MS} ms). الأبطأ: {worst}"))
    if t["error_pct"] > settings.HEALTH_ERROR_PCT:
        out.append(("app:errors", f"أخطاء في التطبيق: {t['error_pct']:.1f}٪ من {t['count']} طلب خلال "
                                  f"{snapshot['minutes']} دقائق (الحد {settings.HEALTH_ERROR_PCT}٪)"))
    return out

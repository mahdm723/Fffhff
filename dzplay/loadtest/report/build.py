"""V6 phase 21: one HTML report (Arabic, charts in inline SVG with hover values, every number also in a table) and a
short Markdown summary from the results of loadtest/run.sh.

    python3 loadtest/report/build.py loadtest/results/<run>                     → <run>/report.html + summary.md
    python3 loadtest/report/build.py <before-run> <after-run> --out cmp.html    → the same + before/after tables

Standard library only (it runs on the server too). Colours: the validated reference palette (dataviz), first four
slots in order, with their dark-mode steps.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
from pathlib import Path

NAMES = {"A": "أ. الأساس (100 مستخدم)", "B": "ب. الحمل (500 ← 1,000 ← 2,500)", "C": "ج. الإجهاد حتى الانهيار",
         "D": "د. الذروة المفاجئة (200 ← 3,000)", "E": "هـ. التحمل الطويل (1,000)", "F": "و. أقصى عدد WebSocket",
         "G": "ز. موجة التسجيل والدخول", "S": "فحص الأدوات (10 مستخدمين)"}
SLO = {"read_p95": 300, "read_p99": 800, "write_p95": 600, "error_pct": 1.0, "cpu_pct": 75, "mem_pct": 80}
SERVER = ("app", "db", "caddy", "redis")
esc = html.escape


# ------------------------------------------------------------------------------------------- loading
def _csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class Scenario:
    def __init__(self, path: Path) -> None:
        self.path, self.key = path, path.name
        self.summary = _json(path / "summary.json") or {}
        self.metrics = self.summary.get("metrics", {})
        self.ts = _csv(path / "timeseries.csv")
        self.sys = _csv(path / "system.csv")
        self.pg = _csv(path / "pg_top.csv")
        self.server = _json(path / "server_endpoints.json") or {}
        self.brk = _json(path / "break.json")
        self.ws = _json(path / "wsflood.json")
        self.errors = (path / "app_errors.txt").read_text(encoding="utf-8").strip() if (path / "app_errors.txt").exists() else ""
        self.stopped = (path / "STOP").read_text(encoding="utf-8").strip() if (path / "STOP").exists() else ""
        self.mock = (path / "mock_calls.txt").read_text(encoding="utf-8").strip() if (path / "mock_calls.txt").exists() else ""

    # k6 summary values ("http_req_duration{kind:read}" → {"p(95)": …})
    def m(self, name: str, stat: str, default=None):
        v = self.metrics.get(name, {}).get("values", {}).get(stat)
        return default if v is None else v

    def levels(self) -> list[str]:
        out = []
        for name in self.metrics:
            if name.startswith("http_reqs{level:"):
                out.append(name[len("http_reqs{level:"):-1])
        order = {"background": -1, "ramp": 10**9}
        return sorted(out, key=lambda x: order.get(x, _f(x)))

    def sys_series(self, source: str, key: str) -> list[tuple[float, float]]:
        return [(_f(r["t"]), _f(r["value"])) for r in self.sys if r["source"] == source and r["key"] == key]

    def ts_series(self, group: str, key: str, col: str) -> list[tuple[float, float]]:
        return [(_f(r["t"]), _f(r[col])) for r in self.ts if r["group"] == group and r["key"] == key and r[col] != ""]

    def stat(self, source: str, key: str, fn=max) -> float | None:
        vals = [v for _, v in self.sys_series(source, key)]
        return round(fn(vals), 1) if vals else None

    def row(self) -> dict:
        dur = self.summary.get("state", {}).get("testRunDurationMs", 0) / 1000
        reqs = self.m("http_reqs", "count", 0)
        fail = self.m("http_req_failed", "rate", 0) or 0
        mem_limit = self.stat("app", "mem_limit_mb") or 0
        app_mem = self.stat("app", "mem_mb")
        return {
            "users": int(self.m("vus_max", "max", 0) or self.m("vus", "max", 0) or 0),
            "duration_min": round(dur / 60, 1), "rps": round(reqs / dur, 1) if dur else 0,
            "read_p50": self.m("http_req_duration{kind:read}", "med"), "read_p95": self.m("http_req_duration{kind:read}", "p(95)"),
            "read_p99": self.m("http_req_duration{kind:read}", "p(99)"), "write_p95": self.m("http_req_duration{kind:write}", "p(95)"),
            "write_p99": self.m("http_req_duration{kind:write}", "p(99)"),
            "error_pct": round(100 * fail, 2), "limited": int(self.m("rate_limited_429", "count", 0) or 0),
            "refused": int(self.m("refused_4xx", "count", 0) or 0),
            "app_cpu_avg": self.stat("app", "cpu_pct", lambda v: sum(v) / len(v)), "app_cpu_max": self.stat("app", "cpu_pct"),
            "db_cpu_avg": self.stat("db", "cpu_pct", lambda v: sum(v) / len(v)), "db_cpu_max": self.stat("db", "cpu_pct"),
            "caddy_cpu_avg": self.stat("caddy", "cpu_pct", lambda v: sum(v) / len(v)),
            "redis_cpu_avg": self.stat("redis", "cpu_pct", lambda v: sum(v) / len(v)),
            "app_mem_max": app_mem, "app_mem_pct": round(100 * app_mem / mem_limit, 1) if app_mem and mem_limit else None,
            "host_mem_pct": self.stat("host", "mem_used_pct"), "pg_conn_max": self.stat("pg", "conn_total"),
            "ws_dropped": int(self.m("ws_dropped", "count", 0) or 0), "ws_opened": int(self.m("ws_opened", "count", 0) or 0),
        }


# ------------------------------------------------------------------------------------------- formatting
def num(v, digits=0, unit="") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    s = f"{v:,.{digits}f}" if isinstance(v, (int, float)) else str(v)
    return f"\u2066{s}{unit}\u2069"  # left-to-right isolate: "81 ms" stays "81 ms" in Arabic text


def ms(v) -> str:
    return num(v, 0, " ms") if v is None or v >= 10 else num(v, 1, " ms")


def verdict(ok: bool | None) -> str:
    if ok is None:
        return '<span class="tag">—</span>'
    return '<span class="tag tag--good">✓ ضمن الهدف</span>' if ok else '<span class="tag tag--bad">✗ خارج الهدف</span>'


def slo_ok(r: dict) -> bool | None:
    if r["read_p95"] is None:
        return None
    return (r["read_p95"] < SLO["read_p95"] and (r["read_p99"] or 0) < SLO["read_p99"]
            and (r["write_p95"] or 0) < SLO["write_p95"] and r["error_pct"] < SLO["error_pct"]
            and server_cpu_pct(r) < SLO["cpu_pct"] and r["ws_dropped"] == 0)


_CPUS = {"n": 1}


def count_cpus(cpuset: str) -> int:
    n = 0
    for part in (cpuset or "").split(","):
        if "-" in part:
            a, b = part.split("-")
            n += int(b) - int(a) + 1
        elif part.strip():
            n += 1
    return n


def server_cpu_pct(r: dict) -> float:
    """The server's CPU use (% of all its cores): the collector counts per core (200 % = two cores busy)."""
    total = sum(v or 0 for v in (r["app_cpu_avg"], r["db_cpu_avg"], r.get("caddy_cpu_avg"), r.get("redis_cpu_avg")))
    return total / max(_CPUS["n"], 1)


# ------------------------------------------------------------------------------------------- charts (inline SVG)
_chart_id = {"n": 0}


def line_chart(title: str, series: list[tuple[str, list[tuple[float, float]]]], unit: str, t0: float,
               y_max: float | None = None, guide: tuple[float, str] | None = None) -> str:
    series = [(name, pts) for name, pts in series if pts]
    if not series:
        return ""
    _chart_id["n"] += 1
    cid = f"c{_chart_id['n']}"
    W, H, L, R, T, B = 720, 220, 56, 16, 14, 30
    xs = [x for _, pts in series for x, _ in pts]
    x0, x1 = min(xs), max(xs)
    top = max([y for _, pts in series for _, y in pts] + ([guide[0]] if guide else []) + [1])
    top = y_max or _nice(top * 1.08)
    sx = lambda x: L + (W - L - R) * ((x - x0) / ((x1 - x0) or 1))  # noqa: E731
    sy = lambda y: T + (H - T - B) * (1 - min(y, top) / top)  # noqa: E731
    out = [f'<figure class="chart" id="{cid}"><figcaption>{esc(title)}</figcaption>',
           f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(title)}">']
    for i in range(5):
        y = top * i / 4
        out.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{sy(y):.1f}" y2="{sy(y):.1f}"/>'
                   f'<text class="tick" x="{L - 6}" y="{sy(y) + 4:.1f}" text-anchor="end">{_short(y)}</text>')
    span = (x1 - x0) or 1
    step = _nice(span / 6)
    t = math.ceil((x0 - t0) / step) * step
    while t0 + t <= x1:
        x = sx(t0 + t)
        out.append(f'<text class="tick" x="{x:.1f}" y="{H - 8}" text-anchor="middle">{_mmss(t)}</text>')
        t += step
    if guide:
        out.append(f'<line class="guide" x1="{L}" x2="{W - R}" y1="{sy(guide[0]):.1f}" y2="{sy(guide[0]):.1f}"/>'
                   f'<text class="tick" x="{W - R}" y="{sy(guide[0]) - 4:.1f}" text-anchor="end">{esc(guide[1])}</text>')
    data = []
    for i, (name, pts) in enumerate(series):
        d = " ".join(f"{'M' if j == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}" for j, (x, y) in enumerate(pts))
        out.append(f'<path class="line s{i + 1}" d="{d}"/>')
        lx, ly = pts[-1]
        out.append(f'<circle class="dot s{i + 1}" cx="{sx(lx):.1f}" cy="{sy(ly):.1f}" r="4"/>')
        data.append({"name": name, "pts": [[round(x - t0), round(y, 1)] for x, y in pts]})
    out.append(f'<line class="cross" x1="0" x2="0" y1="{T}" y2="{H - B}" visibility="hidden"/>')
    out.append(f'<rect class="hit" x="{L}" y="{T}" width="{W - L - R}" height="{H - T - B}"/></svg>')
    if len(series) > 1:
        out.append('<div class="legend">' + "".join(
            f'<span><i class="key s{i + 1}"></i>{esc(n)}</span>' for i, (n, _) in enumerate(series)) + "</div>")
    out.append('<div class="tip" hidden></div>')
    meta = {"x0": x0 - t0, "x1": x1 - t0, "L": L, "R": R, "W": W, "unit": unit, "series": data}
    out.append(f'<script type="application/json" class="chart-data">{json.dumps(meta)}</script></figure>')
    return "".join(out)


def _nice(v: float) -> float:
    if v <= 0:
        return 1
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * e:
            return m * e
    return 10 * e


def _short(v: float) -> str:
    return f"{v / 1000:g}k" if v >= 10000 else f"{v:,.0f}" if v >= 10 else f"{v:g}"


def _mmss(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


# ------------------------------------------------------------------------------------------- sections
def scenario_section(sc: Scenario) -> str:
    r = sc.row()
    t0 = min([_f(x["t"]) for x in sc.ts] + [_f(x["t"]) for x in sc.sys] or [0])
    parts = [f'<section class="card" id="s-{sc.key}"><h2>{esc(NAMES.get(sc.key, sc.key))}</h2>']
    if sc.stopped:
        parts.append(f'<p class="note note--bad">توقّف التشغيل: <bdi>{esc(sc.stopped)}</bdi></p>')
    if sc.brk:
        parts.append(f'<p class="note"><b>نقطة الانهيار:</b> عند <b>{num(sc.brk["users"])}</b> مستخدم نشط — '
                     f'<bdi dir="ltr">{esc(sc.brk["reason"])}</bdi></p>')
    if sc.ws:
        parts.append(f'<p class="note"><b>أقصى عدد ثابت من اتصالات WebSocket:</b> {num(sc.ws.get("max_stable"))} — '
                     f'<bdi dir="ltr">{esc(sc.ws.get("verdict", ""))}</bdi> (زمن الاتصال p95: {ms(sc.ws.get("connect_p95_ms"))})</p>')
    if sc.key != "F":
        parts.append('<div class="tiles">' + "".join(f'<div class="tile"><span>{a}</span><b>{b}</b></div>' for a, b in [
            ("أقصى عدد مستخدمين", num(r["users"])), ("طلبات/ثانية", num(r["rps"], 1)),
            ("قراءة p95", ms(r["read_p95"])), ("كتابة p95", ms(r["write_p95"])),
            ("الأخطاء", num(r["error_pct"], 2, " %")), ("المعالج (التطبيق) متوسط", num(r["app_cpu_avg"], 0, " %")),
        ]) + "</div>")
    users = sc.ts_series("gauge", "vus", "value")
    parts.append(line_chart("المستخدمون النشطون", [("المستخدمون", users)], "", t0))
    parts.append(line_chart("زمن الاستجابة p95 من جهة المستخدم (ms)",
                            [("قراءة", sc.ts_series("kind", "read", "p95")), ("كتابة", sc.ts_series("kind", "write", "p95")),
                             ("صور", sc.ts_series("kind", "media", "p95"))], "ms", t0, guide=(SLO["read_p95"], "هدف القراءة 300")))
    parts.append(line_chart("استهلاك المعالج (% من نواة واحدة)",
                            [(s, sc.sys_series(s, "cpu_pct")) for s in SERVER], "%", t0))
    parts.append(line_chart("الذاكرة (MB)", [(s, sc.sys_series(s, "mem_mb")) for s in SERVER], "MB", t0))
    parts.append(line_chart("PostgreSQL: الاتصالات والأقفال المنتظرة",
                            [("نشطة", sc.sys_series("pg", "conn_active")), ("كل الاتصالات", sc.sys_series("pg", "conn_total")),
                             ("أقفال منتظرة", sc.sys_series("pg", "locks_waiting"))], "", t0))
    if sc.key == "F":
        parts.append(line_chart("اتصالات WebSocket المفتوحة (من جهة الخادم)", [("مفتوحة", sc.sys_series("server", "ws_open"))], "", t0))
    lv = sc.levels()
    if lv:
        rows = []
        for level in lv:
            n = sc.m(f"http_reqs{{level:{level}}}", "count", 0)
            rows.append(f"<tr><td><bdi>{esc(level)}</bdi></td><td>{num(n)}</td>"
                        f"<td>{ms(sc.m(f'http_req_duration{{kind:read,level:{level}}}', 'med'))}</td>"
                        f"<td>{ms(sc.m(f'http_req_duration{{kind:read,level:{level}}}', 'p(95)'))}</td>"
                        f"<td>{ms(sc.m(f'http_req_duration{{kind:read,level:{level}}}', 'p(99)'))}</td>"
                        f"<td>{ms(sc.m(f'http_req_duration{{kind:write,level:{level}}}', 'p(95)'))}</td>"
                        f"<td>{num(100 * (sc.m(f'http_req_failed{{level:{level}}}', 'rate', 0) or 0), 2, ' %')}</td></tr>")
        parts.append('<h3>حسب المستوى</h3><div class="scroll"><table><thead><tr><th>المستوى</th><th>الطلبات</th>'
                     '<th>قراءة p50</th><th>قراءة p95</th><th>قراءة p99</th><th>كتابة p95</th><th>الأخطاء</th></tr></thead>'
                     f'<tbody>{"".join(rows)}</tbody></table></div>')
    if sc.key == "G":
        parts.append('<div class="tiles">' + "".join(f'<div class="tile"><span>{a}</span><b>{b}</b></div>' for a, b in [
            ("تسجيلات ناجحة", num(sc.m("signups_done", "count", 0))), ("تسجيلات فشلت", num(sc.m("signups_failed", "count", 0))),
            ("دخول ناجح", num(sc.m("logins_done", "count", 0))), ("دخول فشل", num(sc.m("logins_failed", "count", 0))),
            ("التسجيل كاملًا p95", ms(sc.m("signup_total_ms", "p(95)"))), ("وصول الرمز p95", ms(sc.m("signup_code_delivery_ms", "p(95)"))),
            ("الدخول p95", ms(sc.m("http_req_duration{kind:auth}", "p(95)"))),
        ]) + "</div>")
    parts.append(endpoint_table(sc))
    parts.append(query_table(sc))
    extras = []
    if sc.mock:
        extras.append(f"<p class='muted wrap'>نداءات الخدمات الوهمية: <bdi dir='ltr'>{esc(sc.mock.replace(',', ', '))}</bdi></p>")
    if sc.errors:
        extras.append(f"<details><summary>أخطاء غير متوقعة في سجل التطبيق</summary><pre dir='ltr'>{esc(sc.errors[:4000])}</pre></details>")
    parts.extend(extras)
    parts.append("</section>")
    return "".join(parts)


def endpoint_table(sc: Scenario, top: int = 12) -> str:
    eps = (sc.server.get("metrics") or {}).get("endpoints") or []
    eps = [e for e in eps if e["count"] >= 20][:top]
    if not eps:
        return ""
    rows = "".join(f"<tr><td><bdi dir='ltr'>{esc(e['endpoint'])}</bdi></td><td>{num(e['count'])}</td><td>{ms(e['p50_ms'])}</td>"
                   f"<td>{ms(e['p95_ms'])}</td><td>{ms(e['p99_ms'])}</td><td>{num(e['error_pct'], 2, ' %')}</td></tr>" for e in eps)
    return ('<h3>أبطأ نقاط الخادم (زمن التطبيق نفسه)</h3><div class="scroll"><table><thead><tr><th>النقطة</th><th>الطلبات</th>'
            f'<th>p50</th><th>p95</th><th>p99</th><th>5xx</th></tr></thead><tbody>{rows}</tbody></table></div>')


def query_table(sc: Scenario, top: int = 10) -> str:
    if not sc.pg:
        return ""
    rows = "".join(f"<tr><td>{num(_f(q['calls']))}</td><td>{num(_f(q['total_ms']) / 1000, 1, ' s')}</td><td>{ms(_f(q['mean_ms']))}</td>"
                   f"<td>{ms(_f(q['max_ms']))}</td><td class='sql'><code dir='ltr'>{esc(q['query'][:260])}</code></td></tr>"
                   for q in sc.pg[:top])
    return ('<h3>أثقل استعلامات PostgreSQL (الوقت الكلي)</h3><div class="scroll"><table><thead><tr><th>المرات</th><th>الكلي</th>'
            f'<th>المتوسط</th><th>الأقصى</th><th>الاستعلام</th></tr></thead><tbody>{rows}</tbody></table></div>')


def overview_table(scs: list[Scenario]) -> str:
    rows = []
    for sc in scs:
        if sc.key == "F":
            rows.append(f"<tr><td>{esc(NAMES.get(sc.key, sc.key))}</td><td colspan='9'>أقصى عدد ثابت: "
                        f"<b>{num((sc.ws or {}).get('max_stable'))}</b> اتصال</td><td>{verdict(None)}</td></tr>")
            continue
        r = sc.row()
        rows.append(f"<tr><td>{esc(NAMES.get(sc.key, sc.key))}</td><td>{num(r['users'])}</td><td>{num(r['rps'], 1)}</td>"
                    f"<td>{ms(r['read_p50'])}</td><td>{ms(r['read_p95'])}</td><td>{ms(r['read_p99'])}</td><td>{ms(r['write_p95'])}</td>"
                    f"<td>{num(r['error_pct'], 2, ' %')}</td><td>{num(server_cpu_pct(r), 0, ' %')}</td>"
                    f"<td>{num(r['app_mem_max'], 0, ' MB')}</td><td>{verdict(slo_ok(r))}</td></tr>")
    return ('<div class="scroll"><table class="overview"><thead><tr><th>السيناريو</th><th>المستخدمون</th><th>طلب/ث</th><th>قراءة p50</th>'
            '<th>قراءة p95</th><th>قراءة p99</th><th>كتابة p95</th><th>الأخطاء</th><th>معالج الخادم (متوسط)</th><th>ذاكرة التطبيق</th>'
            f'<th>الأهداف</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def compare_table(before: list[Scenario], after: list[Scenario]) -> str:
    b = {s.key: s.row() for s in before if s.key != "F"}
    rows = []
    for sc in after:
        if sc.key not in b:
            continue
        x, y = b[sc.key], sc.row()
        def d(k, fmt=ms):
            return f"{fmt(x[k])} ← <b>{fmt(y[k])}</b>"
        rows.append(f"<tr><td>{esc(NAMES.get(sc.key, sc.key))}</td><td>{d('rps', lambda v: num(v, 1))}</td><td>{d('read_p95')}</td>"
                    f"<td>{d('read_p99')}</td><td>{d('write_p95')}</td><td>{d('error_pct', lambda v: num(v, 2, ' %'))}</td>"
                    f"<td>{d('app_cpu_avg', lambda v: num(v, 0, ' %'))}</td></tr>")
    if not rows:
        return ""
    return ('<section class="card"><h2>قبل الإصلاحات ← بعدها</h2><div class="scroll"><table><thead><tr><th>السيناريو</th>'
            '<th>طلب/ث</th><th>قراءة p95</th><th>قراءة p99</th><th>كتابة p95</th><th>الأخطاء</th><th>المعالج</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></section>')


# ------------------------------------------------------------------------------------------- page
CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;
--axis:#c3c2b7;--border:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--good:#006300;--bad:#b42525}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;
--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;
--good:#0ca30c;--bad:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--good:#0ca30c;--bad:#e66767}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:15px/1.6 system-ui,-apple-system,"Segoe UI",
"Noto Sans Arabic",sans-serif}main{max-width:1000px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}
h2{font-size:19px;margin:0 0 12px}h3{font-size:15px;margin:20px 0 8px;color:var(--ink2)}.muted{color:var(--ink2)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px;margin:18px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:8px 0 6px}
.tile{border:1px solid var(--border);border-radius:10px;padding:10px 12px}.tile span{display:block;color:var(--ink2);font-size:13px}
.tile b{font-size:20px;font-weight:600}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:7px 8px;border-bottom:1px solid var(--grid);text-align:start;vertical-align:top}th{color:var(--ink2);font-weight:600}
td{font-variant-numeric:tabular-nums}td.sql code{font-size:12px;word-break:break-all;color:var(--ink2)}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid var(--border);white-space:nowrap}
.tag--good{color:var(--good)}.tag--bad{color:var(--bad)}.note{border-inline-start:3px solid var(--s1);padding:6px 12px;margin:10px 0}
.note--bad{border-color:var(--bad)}figure.chart{margin:16px 0;direction:ltr;position:relative}
figure.chart figcaption{direction:rtl;font-weight:600;margin-bottom:4px}svg{width:100%;height:auto;display:block}
.grid{stroke:var(--grid);stroke-width:1}.tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.guide{stroke:var(--axis);stroke-width:1}.line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.dot{stroke:var(--surface);stroke-width:2}.s1{stroke:var(--s1)}.s2{stroke:var(--s2)}.s3{stroke:var(--s3)}.s4{stroke:var(--s4)}
circle.s1,i.s1{fill:var(--s1);background:var(--s1)}circle.s2,i.s2{fill:var(--s2);background:var(--s2)}
circle.s3,i.s3{fill:var(--s3);background:var(--s3)}circle.s4,i.s4{fill:var(--s4);background:var(--s4)}
.cross{stroke:var(--axis);stroke-width:1}.hit{fill:transparent;cursor:crosshair}
.legend{direction:rtl;display:flex;flex-wrap:wrap;gap:6px 16px;color:var(--ink2);font-size:13px;margin-top:4px}
.key{display:inline-block;width:14px;height:3px;border-radius:2px;margin-inline-end:6px;vertical-align:middle}
.tip{position:absolute;top:28px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 10px;
font-size:12.5px;pointer-events:none;box-shadow:0 2px 10px rgba(0,0,0,.12);white-space:nowrap;direction:rtl}
pre{white-space:pre-wrap;font-size:12px}details{margin-top:10px}.wrap{overflow-wrap:anywhere}
"""

JS = """
for (const fig of document.querySelectorAll('figure.chart')) {
  const meta = JSON.parse(fig.querySelector('.chart-data').textContent);
  const svg = fig.querySelector('svg'), hit = fig.querySelector('.hit'), cross = fig.querySelector('.cross'), tip = fig.querySelector('.tip');
  const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  hit.addEventListener('pointermove', (ev) => {
    const box = svg.getBoundingClientRect(), k = meta.W / box.width, x = (ev.clientX - box.left) * k;
    const t = meta.x0 + (meta.x1 - meta.x0) * Math.min(1, Math.max(0, (x - meta.L) / (meta.W - meta.L - meta.R)));
    const lines = meta.series.map((s) => {
      let best = s.pts[0];
      for (const p of s.pts) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
      return `${s.name}: ${best[1].toLocaleString()} ${meta.unit}`;
    });
    cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.setAttribute('visibility', 'visible');
    tip.hidden = false; tip.textContent = ''; const head = document.createElement('b'); head.textContent = fmt(t); tip.append(head);
    for (const l of lines) { const d = document.createElement('div'); d.textContent = l; tip.append(d); }
    const left = (ev.clientX - fig.getBoundingClientRect().left);
    tip.style.left = `${Math.min(left + 12, fig.clientWidth - tip.offsetWidth - 4)}px`;
  });
  hit.addEventListener('pointerleave', () => { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); });
}
"""


def page(title: str, intro: str, scs: list[Scenario], before: list[Scenario] | None, meta: dict) -> str:
    seed = meta.get("seed", {}).get("counts", {})
    head = (f'<p class="muted">التشغيل <bdi>{esc(str(meta.get("label", "")))}</bdi> · <bdi dir="ltr">{esc(str(meta.get("started", "")))}</bdi> · '
            f'الوتيرة: {esc(str(meta.get("pace", "")))} · عمال uvicorn: {esc(str(meta.get("web_concurrency", "1")))} · '
            f'معالجات الخادم: <bdi dir="ltr">{esc(str(meta.get("server_cpus") or "كلها"))}</bdi></p>')
    seed_line = ""
    if seed:
        seed_line = ("<p class='muted'>البيانات: " + " · ".join(f"<bdi dir='ltr'>{esc(k)} {num(v)}</bdi>" for k, v in seed.items() if v)
                     + "</p>")
    body = [f"<h1>{esc(title)}</h1>", head, seed_line, intro,
            f'<section class="card"><h2>ملخص السيناريوهات</h2>{overview_table(scs)}'
            f'<p class="muted">الأهداف: قراءة <bdi dir="ltr">p95 &lt; {SLO["read_p95"]} ms</bdi> و<bdi dir="ltr">p99 &lt; '
            f'{SLO["read_p99"]} ms</bdi>، كتابة <bdi dir="ltr">p95 &lt; {SLO["write_p95"]} ms</bdi>، أخطاء <bdi dir="ltr">&lt; 1 %</bdi>، '
            'معالج الخادم <bdi dir="ltr">&lt; 75 %</bdi>، بلا انقطاع WebSocket. الأزمنة من جهة المستخدم (k6) وتشمل TLS وCaddy.</p></section>']
    if before:
        body.append(compare_table(before, scs))
    body += [scenario_section(sc) for sc in scs]
    return (f'<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>'
            f'<style>{CSS}</style></head><body><main>{"".join(body)}</main><script>{JS}</script></body></html>')


def summary_md(scs: list[Scenario], meta: dict) -> str:
    lines = [f"# ملخص التشغيل {meta.get('label', '')}", "",
             "| السيناريو | المستخدمون | طلب/ث | قراءة p95 | قراءة p99 | كتابة p95 | الأخطاء | المعالج (متوسط) | الأهداف |",
             "|---|---|---|---|---|---|---|---|---|"]
    for sc in scs:
        if sc.key == "F":
            lines.append(f"| {NAMES['F']} | أقصى ثابت: {num((sc.ws or {}).get('max_stable'))} | | | | | | | |")
            continue
        r = sc.row()
        ok = slo_ok(r)
        lines.append(f"| {NAMES.get(sc.key, sc.key)} | {num(r['users'])} | {num(r['rps'], 1)} | {ms(r['read_p95'])} | {ms(r['read_p99'])} "
                     f"| {ms(r['write_p95'])} | {num(r['error_pct'], 2, ' %')} | {num(r['app_cpu_avg'], 0, ' %')} | "
                     f"{'✓' if ok else '✗' if ok is not None else '—'} |")
        if sc.brk:
            lines.append(f"|  ↳ نقطة الانهيار | {num(sc.brk['users'])} | | {ms(sc.brk['p95_ms'])} | | | {num(sc.brk['error_pct'], 2, ' %')} | | |")
    return "\n".join(lines) + "\n"


def load(run: Path) -> tuple[list[Scenario], dict]:
    meta = _json(run / "meta.json") or {}
    scs = [Scenario(p) for p in sorted(run.iterdir()) if p.is_dir() and len(p.name) == 1]
    return scs, meta


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("runs", nargs="+", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--title", default="اختبار الحمل — staging")
    p.add_argument("--intro", default="")
    a = p.parse_args()
    after = a.runs[-1]
    scs, meta = load(after)
    host_cpus = next((int(_f(r["value"])) for sc in scs for r in sc.sys if r["source"] == "host" and r["key"] == "cpus"), 1)
    _CPUS["n"] = count_cpus(str(meta.get("server_cpus") or "")) or host_cpus
    before = load(a.runs[0])[0] if len(a.runs) > 1 else None
    out = a.out or (after / "report.html")
    out.write_text(page(a.title, a.intro, scs, before, meta), encoding="utf-8")
    (after / "summary.md").write_text(summary_md(scs, meta), encoding="utf-8")
    print(f"report → {out}")


if __name__ == "__main__":
    main()

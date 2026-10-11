"""V6 phase 12: collect every interface text of the code into app/i18n/msgids.json.

    cd dzplay && .venv/bin/python -m app.i18n.extract           # write msgids.json, list what lacks a translation
    cd dzplay && .venv/bin/python -m app.i18n.extract --check   # exit 1 if msgids.json is stale or a translation is missing

Sources:
* the interface: t('…') / tNodes('…') / N_('…') in static/js/**/*.js and static/sw.js (the first argument must
  be a literal; variables are written {name} inside it, never ${…});
* the server: tr('…'), N_('…'), AppError(status, code, '…'), rate_limited(x, '…') and _error(status, code, '…')
  with a literal text, in app/**/*.py — except the panel's own modules (the panel stays in Arabic).
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from app import i18n

ROOT = i18n.HERE.parent.parent  # dzplay/
OUT = i18n.HERE / "msgids.json"
ARABIC = re.compile(r"[؀-ۿ]")
# panel-only server code: its messages are read by the admin, in Arabic
SERVER_SKIP = ("app/api/admin", "app/services/admin", "app/i18n/", "app/admin_cli.py", "app/services/tunables.py",
               "app/services/runtime_config.py", "app/services/moderation_words.py")
JS_CALL = re.compile(r"(?<![\w$.])(?:t|tNodes|N_)\(\s*")


def _js_string(src: str, i: int) -> tuple[str, int] | None:
    """The JS string literal starting at src[i] (quote included), unescaped, and the index after it."""
    quote = src[i]
    if quote not in "'\"`":
        return None
    out, j = [], i + 1
    while j < len(src):
        c = src[j]
        if c == "\\":
            nxt = src[j + 1]
            if nxt == "u":
                if src[j + 2] == "{":
                    end = src.index("}", j)
                    out.append(chr(int(src[j + 3:end], 16)))
                    j = end + 1
                    continue
                out.append(chr(int(src[j + 2:j + 6], 16)))
                j += 6
                continue
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            j += 2
            continue
        if c == quote:
            return "".join(out), j + 1
        if quote == "`" and c == "$" and src[j + 1] == "{":
            raise ValueError("${…} inside t(`…`): write {name} and pass the value as a parameter")
        out.append(c)
        j += 1
    return None


_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")


def _mask_comments(src: str) -> str:
    """src with its // and /* */ comments blanked (same length, newlines kept): examples in comments are not texts."""
    out, i, n, prev = list(src), 0, len(src), ""
    while i < n:
        c = src[i]
        if c == "/" and src[i + 1:i + 2] in ("/", "*"):
            end = src.find("\n", i) if src[i + 1] == "/" else src.find("*/", i + 2) + 2
            end = n if end < (1 if src[i + 1] == "/" else 2) else end
            for k in range(i, end):
                if out[k] != "\n":
                    out[k] = " "
            i = end
            continue
        if c == "/" and (prev == "" or prev in _REGEX_PREV or re.search(r"\b(return|typeof|case)\s*$", src[max(0, i - 10):i])):
            j, in_class = i + 1, False  # a regex literal
            while j < n and src[j] != "\n":
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                elif src[j] == "/" and not in_class:
                    break
                j += 1
            i, prev = j + 1, "x"
            continue
        if c in "'\"`":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            i, prev = j + 1, "x"
            continue
        if not c.isspace():
            prev = "x" if c in ")]" else c if c in _REGEX_PREV else "x"
        i += 1
    return "".join(out)


def scan_js(path: Path, found: dict) -> None:
    src = _mask_comments(path.read_text(encoding="utf-8"))
    rel = path.relative_to(ROOT).as_posix()
    for m in JS_CALL.finditer(src):
        start = m.end()
        lit = _js_string(src, start)
        if lit is None:
            continue
        text, end = lit
        line = src.count("\n", 0, start) + 1
        plural = bool(re.match(r"\s*,\s*\{[^}]*\bn\s*[:,}]", src[end:end + 80]))
        _add(found, text, f"{rel}:{line}", client=True, plural=plural)


def _const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def scan_py(path: Path, found: dict) -> None:
    rel = path.relative_to(ROOT).as_posix()
    if rel.startswith(SERVER_SKIP):
        return
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        arg = None
        if name in ("tr", "N_", "tr_params") and node.args:
            arg = node.args[0]
        elif name in ("AppError", "_error") and len(node.args) >= 3:
            arg = node.args[2]
        elif name == "rate_limited" and len(node.args) >= 2:
            arg = node.args[1]
        for kw in node.keywords:
            if kw.arg == "message" and name in ("AppError", "rate_limited"):
                arg = kw.value
        text = _const(arg) if arg is not None else None
        if text and ARABIC.search(text):
            dicts = [kw.value for kw in node.keywords if kw.arg == "params"] + [a for a in node.args[1:] if isinstance(a, ast.Dict)]
            plural = any(kw.arg == "n" for kw in node.keywords) or any(
                isinstance(d, ast.Dict) and any(_const(k) == "n" for k in d.keys) for d in dicts)
            _add(found, text, f"{rel}:{node.lineno}", client=False, plural=plural)


def _add(found: dict, text: str, where: str, *, client: bool, plural: bool) -> None:
    entry = found.setdefault(text, {"id": text, "where": [], "client": False, "plural": False})
    entry["where"].append(where)
    entry["client"] = entry["client"] or client
    entry["plural"] = entry["plural"] or plural


def collect() -> list[dict]:
    found: dict[str, dict] = {}
    for path in sorted((ROOT / "static" / "js").rglob("*.js")) + [ROOT / "static" / "sw.js"]:
        scan_js(path, found)
    for path in sorted((ROOT / "app").rglob("*.py")):
        scan_py(path, found)
    # the fixed server answers (errors.py helpers, main.py handlers)
    for text in ("غير موجود.", "محاولات كثيرة. حاول مرة أخرى بعد قليل."):
        found.setdefault(text, {"id": text, "where": ["app/errors.py"], "client": False, "plural": False})
    # the web app manifest's description (served translated by main.manifest)
    desc = json.loads((ROOT / "static" / "manifest.webmanifest").read_text(encoding="utf-8")).get("description")
    if desc:
        _add(found, desc, "static/manifest.webmanifest", client=False, plural=False)
    return sorted(found.values(), key=lambda e: e["id"])


def missing(entries: list[dict]) -> dict[str, list[str]]:
    out = {}
    for lang in i18n.LANGS:
        if lang == i18n.DEFAULT:
            continue
        table = i18n.defaults(lang)
        out[lang] = [e["id"] for e in entries if e["id"] not in table]
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    entries = collect()
    text = json.dumps(entries, ensure_ascii=False, indent=1) + "\n"
    gaps = missing(entries)
    if "--check" in argv:
        stale = not OUT.exists() or OUT.read_text(encoding="utf-8") != text
        if stale:
            print("app/i18n/msgids.json is stale: run python -m app.i18n.extract")
        for lang, ids in gaps.items():
            if ids:
                print(f"{lang}: {len(ids)} text(s) without a translation, e.g. {ids[0]!r}")
        return 1 if stale or any(gaps.values()) else 0
    OUT.write_text(text, encoding="utf-8")
    print(f"{len(entries)} texts ({sum(e['client'] for e in entries)} in the interface) → {OUT.relative_to(ROOT)}")
    for lang, ids in gaps.items():
        print(f"  {lang}: {len(ids)} without a translation")
    return 0


if __name__ == "__main__":
    sys.exit(main())

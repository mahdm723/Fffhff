"""V6 phase 12: interface texts edited in the panel (i18n_texts) — load, edit, history, restore, export and import.

Every change writes a revision first (the backup), then updates the cached OVERRIDES of this process; the API layer
broadcasts {"type": "i18n"} so the other processes reload and the open apps fetch their dictionary again."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import clock, i18n
from app.errors import AppError
from app.models import I18nRevision, I18nText

CATALOG = i18n.HERE / "msgids.json"
MAX_TEXT = 4000


def key_of(msgid: str) -> str:
    return hashlib.sha1(msgid.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def catalog() -> tuple[dict, ...]:
    """Every interface text of the code (app/i18n/extract.py): msgid, where it is used, client or server, plural."""
    if not CATALOG.exists():
        return ()
    return tuple(json.loads(CATALOG.read_text(encoding="utf-8")))


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _decode(raw: str | None) -> object | None:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def load(db: Session) -> None:
    data: dict[str, dict[str, object]] = {lang: {} for lang in i18n.LANGS}
    for row in db.scalars(select(I18nText)):
        if row.lang in data:
            data[row.lang][row.msgid] = _decode(row.text)
    i18n.OVERRIDES.clear()
    i18n.OVERRIDES.update(data)
    client_messages.cache_clear()


def _clean(value: object, plural: bool) -> object:
    if isinstance(value, dict):
        forms = {k: str(v).strip() for k, v in value.items() if k in ("zero", "one", "two", "few", "many", "other") and str(v).strip()}
        if "other" not in forms:
            raise AppError(400, "invalid_input", "صيغة الجمع تحتاج إلى «other» على الأقل.")
        if any(len(v) > MAX_TEXT for v in forms.values()):
            raise AppError(400, "invalid_input", "النص طويل جدًا.")
        return forms
    text = str(value or "").strip()
    if not text:
        raise AppError(400, "invalid_input", "النص فارغ. استعمل «الرجوع إلى الافتراضي» بدل ذلك.")
    if len(text) > MAX_TEXT:
        raise AppError(400, "invalid_input", "النص طويل جدًا.")
    return text


@lru_cache(maxsize=1)
def _keys() -> dict[str, str]:
    return {key_of(e["id"]): e["id"] for e in catalog()}


def msgid_of(key: str) -> str:
    """The text behind a row key of the panel table (sha1 of the Arabic text)."""
    msgid = _keys().get(key) if isinstance(key, str) else None
    if msgid is None:
        raise AppError(404, "not_found", "غير موجود.")
    return msgid


def _known(msgid: str) -> dict:
    for entry in catalog():
        if entry["id"] == msgid:
            return entry
    raise AppError(404, "not_found", "غير موجود.")


def set_text(db: Session, lang: str, msgid: str, value: object, actor: str, note: str | None = None) -> None:
    if lang not in i18n.LANGS:
        raise AppError(400, "invalid_input", "لغة غير معروفة.")
    entry = _known(msgid)
    value = _clean(value, entry.get("plural", False))
    key = key_of(msgid)
    row = db.get(I18nText, (lang, key))
    if row is None:
        row = I18nText(lang=lang, key=key, msgid=msgid, text="")
        db.add(row)
    row.text, row.updated_at, row.updated_by = _encode(value), clock.utcnow(), actor
    db.add(I18nRevision(lang=lang, key=key, msgid=msgid, text=row.text, created_by=actor, note=(note or "")[:300] or None))
    db.flush()
    i18n.OVERRIDES.setdefault(lang, {})[msgid] = value
    client_messages.cache_clear()


def reset_text(db: Session, lang: str, msgid: str, actor: str, note: str | None = None) -> None:
    if lang not in i18n.LANGS:
        raise AppError(400, "invalid_input", "لغة غير معروفة.")
    _known(msgid)
    key = key_of(msgid)
    db.execute(delete(I18nText).where(I18nText.lang == lang, I18nText.key == key))
    db.add(I18nRevision(lang=lang, key=key, msgid=msgid, text=None, created_by=actor, note=note or "الرجوع إلى الافتراضي"))
    db.flush()
    i18n.OVERRIDES.get(lang, {}).pop(msgid, None)
    client_messages.cache_clear()


def revisions(db: Session, lang: str, msgid: str, limit: int = 30) -> list[dict]:
    rows = db.scalars(select(I18nRevision).where(I18nRevision.lang == lang, I18nRevision.key == key_of(msgid))
                      .order_by(I18nRevision.id.desc()).limit(limit))
    return [{"id": r.id, "text": _decode(r.text), "at": r.created_at.isoformat() + "Z", "by": r.created_by, "note": r.note}
            for r in rows]


def restore(db: Session, revision_id: int, actor: str) -> dict:
    rev = db.get(I18nRevision, revision_id)
    if rev is None:
        raise AppError(404, "not_found", "غير موجود.")
    note = f"استرجاع النسخة #{rev.id}"
    if rev.text is None:
        reset_text(db, rev.lang, rev.msgid, actor, note)
    else:
        set_text(db, rev.lang, rev.msgid, _decode(rev.text), actor, note)
    return {"lang": rev.lang, "msgid": rev.msgid}


def effective(msgid: str, lang: str) -> object | None:
    """The text shown in lang (panel edit or shipped default), None when it falls back to Arabic."""
    return i18n.lookup(msgid, lang)


def listing(lang_filter: str | None = None, q: str = "", missing: str | None = None, edited: bool = False,
            offset: int = 0, limit: int = 100) -> dict:
    """The panel table: every text with its three languages side by side. Filters: a search (any language),
    «missing in fr / en», «edited in the panel»; pages of `limit` rows."""
    q = (q or "").strip().lower()
    rows, missing_count = [], {lang: 0 for lang in i18n.LANGS if lang != i18n.DEFAULT}
    for entry in catalog():
        msgid = entry["id"]
        cells = {}
        for lang in i18n.LANGS:
            edited = msgid in i18n.OVERRIDES.get(lang, {})
            value = effective(msgid, lang)
            cells[lang] = {"text": value, "edited": edited, "default": i18n.defaults(lang).get(msgid)}
            if lang != i18n.DEFAULT and value is None:
                missing_count[lang] += 1
        if missing and cells.get(missing, {}).get("text") is not None:
            continue
        if edited and not any(c["edited"] for c in cells.values()):
            continue
        if q and q not in msgid.lower() and not any(q in json.dumps(c["text"], ensure_ascii=False).lower()
                                                    for c in cells.values() if c["text"] is not None):
            continue
        rows.append({"key": key_of(msgid), "msgid": msgid, "where": entry.get("where", [])[:5],
                     "client": entry.get("client", False), "plural": entry.get("plural", False), "cells": cells})
    offset, limit = max(0, int(offset)), max(1, min(int(limit), 200))
    return {"total": len(catalog()), "missing": missing_count, "rows": rows[offset:offset + limit], "shown": len(rows),
            "offset": offset, "limit": limit}


def detail(db: Session, lang: str, key: str) -> dict:
    """One text in one language for the editor: the Arabic original, the shipped default, the edit, the history."""
    if lang not in i18n.LANGS:
        raise AppError(400, "invalid_input", "لغة غير معروفة.")
    msgid = msgid_of(key)
    entry = _known(msgid)
    return {"key": key, "lang": lang, "msgid": msgid, "plural": entry.get("plural", False), "where": entry.get("where", [])[:8],
            "client": entry.get("client", False), "text": effective(msgid, lang), "default": i18n.defaults(lang).get(msgid),
            "edited": msgid in i18n.OVERRIDES.get(lang, {}), "revisions": revisions(db, lang, msgid)}


@lru_cache(maxsize=8)
def client_messages(lang: str) -> dict:
    """The dictionary the app downloads: every text used in the interface, in lang (Arabic: only edits and plurals)."""
    messages = {}
    for entry in catalog():
        if not entry.get("client"):
            continue
        value = i18n.lookup(entry["id"], lang)
        if value is not None:
            messages[entry["id"]] = value
    version = hashlib.sha1(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
    return {"lang": lang, "dir": i18n.DIRS[lang], "locale": i18n.LOCALES[lang], "version": version, "messages": messages}


def _content_rows(db: Session, settings, lang: str) -> dict:
    from app.services import cms

    out = {}
    for key, sp in cms.registry().items():
        cur = cms.current(db, settings, key, lang)
        if not cur["missing"]:
            out[key] = {"title": cur["title"] if sp.kind != "text" else None, "body": cur["body"]}
    return out


def export(lang: str, db: Session | None = None, settings=None) -> dict:
    """A translation file: every interface text of lang (edits included) and, with a database, the content system's
    texts (policies, short texts, e-mails) — {"format": "dalta-i18n/1", "lang", "messages", "content"}."""
    if lang not in i18n.LANGS:
        raise AppError(400, "invalid_input", "لغة غير معروفة.")
    messages = {}
    for entry in catalog():
        value = effective(entry["id"], lang)
        if value is not None:
            messages[entry["id"]] = value
    out = {"format": "dalta-i18n/1", "lang": lang, "count": len(messages), "messages": messages}
    if db is not None:
        out["content"] = _content_rows(db, settings, lang)
    return out


def _file(data: object) -> tuple[dict, dict]:
    if not isinstance(data, dict):
        raise AppError(400, "invalid_input", "ملف غير صالح: يجب أن يكون JSON على شكل {النص العربي: الترجمة}.")
    if "messages" in data or "content" in data:
        messages, content = data.get("messages") or {}, data.get("content") or {}
    else:
        messages, content = data, {}
    if not isinstance(messages, dict) or not isinstance(content, dict):
        raise AppError(400, "invalid_input", "ملف غير صالح: يجب أن يكون JSON على شكل {النص العربي: الترجمة}.")
    return messages, content


def import_preview(lang: str, data: dict, db: Session | None = None, settings=None) -> dict:
    """What an import would change (nothing is written): changed, unchanged and unknown texts and content keys."""
    if lang not in i18n.LANGS:
        raise AppError(400, "invalid_input", "لغة غير معروفة.")
    messages, content = _file(data)
    if isinstance(data, dict) and data.get("lang") not in (None, lang):
        raise AppError(400, "invalid_input", "لغة الملف لا تطابق اللغة المختارة.")
    known = {e["id"]: e for e in catalog()}
    changes, unknown, same = [], [], 0
    for msgid, value in messages.items():
        if msgid not in known:
            unknown.append(msgid)
            continue
        if not isinstance(value, (str, dict)) or (isinstance(value, str) and not value.strip()):
            continue
        old = effective(msgid, lang)
        if old == value:
            same += 1
            continue
        changes.append({"msgid": msgid, "old": old, "new": value})
    content_changes = []
    if content and db is not None:
        from app.services import cms

        registry = cms.registry()
        for key, value in content.items():
            if key not in registry or not isinstance(value, dict) or not isinstance(value.get("body"), str):
                unknown.append(str(key)[:64])
                continue
            cur = cms.current(db, settings, key, lang)
            title = value.get("title") if isinstance(value.get("title"), str) else None
            if not cur["missing"] and cur["body"] == value["body"] and (title is None or title == cur["title"]):
                same += 1
                continue
            content_changes.append({"key": key, "name": registry[key].title, "title": title, "body": value["body"]})
    return {"lang": lang, "changes": changes, "content_changes": content_changes, "unchanged": same,
            "unknown": unknown[:50], "unknown_count": len(unknown)}


def import_apply(db: Session, lang: str, data: dict, actor: str, settings=None) -> dict:
    """Apply an import: every change is saved like an edit from the panel, with its revision (the backup)."""
    preview = import_preview(lang, data, db, settings)
    for change in preview["changes"]:
        set_text(db, lang, change["msgid"], change["new"], actor, "استيراد ملف")
    if preview["content_changes"]:
        from app.services import cms

        for change in preview["content_changes"]:
            cms.save(db, settings, change["key"], body=change["body"], title=change["title"], note="استيراد ملف",
                     actor=actor, lang=lang)
    return {"applied": len(preview["changes"]), "content_applied": len(preview["content_changes"]),
            "unchanged": preview["unchanged"], "unknown_count": preview["unknown_count"]}

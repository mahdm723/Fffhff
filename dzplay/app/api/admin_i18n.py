"""V6 phase 12 admin: «اللغات والنصوص» — every interface text in Arabic, French and English side by side: search,
«missing translation» filter, edit (plural forms too), history with restore, back to the shipped default, export
and import of a translation file (JSON, with a preview first). Every change is audited, keeps a revision (the
backup) and is broadcast so the other instances reload and the open apps redraw at once — no deployment needed.
The panel itself stays in Arabic. Mounted under ADMIN_PATH."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field

from app.api.admin import SUPER_ADMIN, AdminContext, _record
from app.api.deps import get_state
from app.api.schemas import _Body
from app.i18n import store

router = APIRouter(prefix="/api/admin", tags=["admin-i18n"])


class TextBody(_Body):
    text: str | dict[str, str] = Field(...)
    note: str | None = Field(default=None, max_length=300)


class RestoreBody(_Body):
    revision_id: int


class ImportBody(_Body):
    lang: str = Field(max_length=5)
    data: dict[str, Any]


def broadcast(request: Request) -> None:
    """Every instance reloads its texts (this one included) and the open apps fetch their dictionary again."""
    get_state(request).hub.notify_system({"type": "i18n"})


@router.get("/i18n")
def texts(request: Request, q: str = "", missing: str = "", edited: bool = False, offset: int = 0, limit: int = 100,
          ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    return store.listing(q=q[:200], missing=missing if missing in ("fr", "en") else None, edited=edited,
                         offset=offset, limit=limit)


@router.get("/i18n/export")
def export(request: Request, lang: str, ac: AdminContext = Depends(SUPER_ADMIN)) -> Response:
    import json

    st = get_state(request)
    with st.database.session() as db:
        data = store.export(lang, db, st.settings)
        _record(db, ac, "i18n_export", target_type="i18n", target_id=lang, detail=f"{data['count']} texts")
    return Response(json.dumps(data, ensure_ascii=False, indent=1), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="dalta-{lang}.json"', "Cache-Control": "no-store"})


@router.post("/i18n/import/preview")
def import_preview(body: ImportBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return store.import_preview(body.lang, body.data, db, st.settings)


@router.post("/i18n/import")
def import_apply(body: ImportBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        out = store.import_apply(db, body.lang, body.data, ac.actor, st.settings)
        _record(db, ac, "i18n_import", target_type="i18n", target_id=body.lang,
                detail=f"{out['applied']} texts, {out['content_applied']} content")
    broadcast(request)
    return out


@router.post("/i18n/restore")
def restore(body: RestoreBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        out = store.restore(db, body.revision_id, ac.actor)
        _record(db, ac, "i18n_restore", target_type="i18n", target_id=out["lang"], detail=f"revision={body.revision_id}")
    broadcast(request)
    with st.database.session() as db:
        return store.detail(db, out["lang"], store.key_of(out["msgid"]))


@router.get("/i18n/{lang}/{key}")
def text_detail(lang: str, key: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return store.detail(db, lang, key)


@router.put("/i18n/{lang}/{key}")
def text_save(lang: str, key: str, body: TextBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        msgid = store.msgid_of(key)
        store.set_text(db, lang, msgid, body.text, ac.actor, body.note)
        _record(db, ac, "i18n_update", target_type="i18n", target_id=f"{lang}:{key[:12]}", detail=msgid[:120])
    broadcast(request)
    with st.database.session() as db:
        return store.detail(db, lang, key)


@router.delete("/i18n/{lang}/{key}")
def text_reset(lang: str, key: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        msgid = store.msgid_of(key)
        store.reset_text(db, lang, msgid, ac.actor)
        _record(db, ac, "i18n_reset", target_type="i18n", target_id=f"{lang}:{key[:12]}", detail=msgid[:120])
    broadcast(request)
    with st.database.session() as db:
        return store.detail(db, lang, key)

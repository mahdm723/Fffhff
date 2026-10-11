"""V6 phase 8 admin: the content system — list, edit (with preview), revision history and restore, back to the
default text. A «major change» to the privacy policy, the terms or the guidelines needs a fresh 2FA code and asks
every user to accept the new version. Mounted under ADMIN_PATH; every change is audited.

V6 phase 12: every text in Arabic, French and English (?lang= / "lang"); a «major change» is made on the Arabic
original. Each change is broadcast so the open apps redraw with it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from app.api.admin import SUPER_ADMIN, AdminContext, _record
from app.api.deps import get_state
from app.api.schemas import _Body
from app.services import admin_auth, cms

router = APIRouter(prefix="/api/admin", tags=["admin-content"])


class SaveBody(_Body):
    body: str = Field(max_length=60_000)
    lang: str | None = Field(default=None, max_length=5)
    title: str | None = Field(default=None, max_length=300)
    note: str | None = Field(default=None, max_length=300)
    major: bool = False
    code: str | None = Field(default=None, max_length=12)


class PreviewBody(_Body):
    body: str = Field(max_length=60_000)
    lang: str | None = Field(default=None, max_length=5)


class RestoreBody(_Body):
    revision_id: int


@router.get("/content")
def content_list(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return {"items": cms.listing(db, st.settings), "ack_version": cms.ack_version(db)}


def _view(db, st, key: str, lang: str | None) -> dict:
    code = cms._edit_lang(lang)
    return {**cms.current(db, st.settings, key, code), "revisions": cms.revisions(db, key, lang=code), "ack": cms.spec(key).ack}


def _broadcast(request: Request) -> None:
    from app.api.admin_i18n import broadcast

    broadcast(request)


@router.get("/content/{key}")
def content_get(key: str, request: Request, lang: str | None = None, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return _view(db, st, key, lang)


@router.post("/content/{key}/preview")
def content_preview(key: str, body: PreviewBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return cms.preview(db, st.settings, key, body.body, body.lang)


@router.put("/content/{key}")
def content_save(key: str, body: SaveBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    cms.spec(key)
    lang = cms._edit_lang(body.lang)
    major = body.major and lang == "ar"  # re-consent follows the Arabic original
    if major:  # re-consent for everyone: confirmed with a fresh 2FA code
        with st.database.session() as db:
            admin_auth.verify_step_up(db, st.settings, ac.client, ac.admin, body.code or "")
    with st.database.session() as db:
        out = cms.save(db, st.settings, key, body=body.body, title=body.title, note=body.note, major=major,
                       actor=ac.actor, lang=lang)
        _record(db, ac, "content_major" if major else "content_update", target_type="content",
                target_id=key if lang == "ar" else f"{key}:{lang}",
                detail=f"ack_version={out['ack_version']}" if out["ack_version"] else None)
    _broadcast(request)
    with st.database.session() as db:
        return {**_view(db, st, key, lang), "ack_version": out["ack_version"]}


@router.post("/content/{key}/restore")
def content_restore(key: str, body: RestoreBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        lang = cms.restore(db, st.settings, key, body.revision_id, ac.actor)
        _record(db, ac, "content_restore", target_type="content", target_id=key if lang == "ar" else f"{key}:{lang}",
                detail=f"revision={body.revision_id}")
    _broadcast(request)
    with st.database.session() as db:
        return _view(db, st, key, lang)


@router.post("/content/{key}/reset")
def content_reset(key: str, request: Request, lang: str | None = None, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    code = cms._edit_lang(lang)
    with st.database.session() as db:
        cms.reset(db, key, ac.actor, code)
        _record(db, ac, "content_reset", target_type="content", target_id=key if code == "ar" else f"{key}:{code}")
    _broadcast(request)
    with st.database.session() as db:
        return _view(db, st, key, code)

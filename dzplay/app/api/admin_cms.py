"""V6 phase 8 admin: the content system — list, edit (with preview), revision history and restore, back to the
default text. A «major change» to the privacy policy, the terms or the guidelines needs a fresh 2FA code and asks
every user to accept the new version. Mounted under ADMIN_PATH; every change is audited."""

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
    title: str | None = Field(default=None, max_length=300)
    note: str | None = Field(default=None, max_length=300)
    major: bool = False
    code: str | None = Field(default=None, max_length=12)


class PreviewBody(_Body):
    body: str = Field(max_length=60_000)


class RestoreBody(_Body):
    revision_id: int


@router.get("/content")
def content_list(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return {"items": cms.listing(db), "ack_version": cms.ack_version(db)}


@router.get("/content/{key}")
def content_get(key: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        cur = cms.current(db, st.settings, key)
        return {**cur, "revisions": cms.revisions(db, key), "ack": cms.spec(key).ack}


@router.post("/content/{key}/preview")
def content_preview(key: str, body: PreviewBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return cms.preview(db, st.settings, key, body.body)


@router.put("/content/{key}")
def content_save(key: str, body: SaveBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    sp = cms.spec(key)
    if body.major:  # re-consent for everyone: confirmed with a fresh 2FA code
        with st.database.session() as db:
            admin_auth.verify_step_up(db, st.settings, ac.client, ac.admin, body.code or "")
    with st.database.session() as db:
        out = cms.save(db, st.settings, key, body=body.body, title=body.title, note=body.note, major=body.major,
                       actor=ac.actor)
        _record(db, ac, "content_major" if body.major else "content_update", target_type="content", target_id=key,
                detail=f"ack_version={out['ack_version']}" if out["ack_version"] else None)
    with st.database.session() as db:
        return {**cms.current(db, st.settings, key), "revisions": cms.revisions(db, key), "ack": sp.ack,
                "ack_version": out["ack_version"]}


@router.post("/content/{key}/restore")
def content_restore(key: str, body: RestoreBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        cms.restore(db, st.settings, key, body.revision_id, ac.actor)
        _record(db, ac, "content_restore", target_type="content", target_id=key, detail=f"revision={body.revision_id}")
    with st.database.session() as db:
        return {**cms.current(db, st.settings, key), "revisions": cms.revisions(db, key)}


@router.post("/content/{key}/reset")
def content_reset(key: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        cms.reset(db, key, ac.actor)
        _record(db, ac, "content_reset", target_type="content", target_id=key)
    with st.database.session() as db:
        return {**cms.current(db, st.settings, key), "revisions": cms.revisions(db, key)}

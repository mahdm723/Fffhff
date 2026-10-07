"""Public ideas API — separate from the private messaging API (/api/messages, /api/conversations)."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import current_user, get_state
from app.api.schemas import ReportBody, SendBody
from app.services import ideas
from app.services.messaging import Effects

router = APIRouter(prefix="/api", tags=["ideas"])


class PostBody(SendBody):
    media_id: str | None = Field(default=None, max_length=40)  # V5: a finished upload (purpose "idea")


class CommentBody(SendBody):
    parent_id: str | None = Field(default=None, max_length=32)  # V6: reply in a thread


class ReactionBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reaction: str | None = Field(default=None, max_length=8)  # "like" | "dislike" | null (remove)


@router.post("/posts", status_code=201)
def create_post(body: PostBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = ideas.create_post(db, st.settings, st.limiter, user, content=body.content, client_id=body.client_id,
                                   media_id=body.media_id, effects=effects)
    st.dispatch(effects)
    return result


@router.get("/posts/feed")
def feed(request: Request, cursor: str | None = Query(default=None, max_length=300),
         limit: int | None = Query(default=None, ge=1, le=50)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        return ideas.feed(db, st.settings, user, cursor=cursor, limit=limit)


@router.get("/posts/{post_id}")
def get_post(post_id: str, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return ideas.get_post(db, current_user(request, db), post_id)


@router.delete("/posts/{post_id}")
def delete_post(post_id: str, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        ideas.delete_post(db, current_user(request, db), post_id, effects)
    st.dispatch(effects)
    return {"ok": True}


@router.put("/posts/{post_id}/reaction")
def set_reaction(post_id: str, body: ReactionBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        return ideas.set_reaction(db, st.settings, st.limiter, user, post_id, body.reaction)


@router.post("/posts/{post_id}/comments", status_code=201)
def add_comment(post_id: str, body: CommentBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = ideas.add_comment(db, st.settings, st.limiter, user, post_id, content=body.content, effects=effects,
                                   parent_id=body.parent_id)
    st.dispatch(effects)
    return result


@router.get("/posts/{post_id}/comments")
def list_comments(post_id: str, request: Request, before: str | None = Query(default=None, max_length=40),
                  limit: int = Query(default=50, ge=1, le=100)) -> dict:
    """V6: public comments for everyone (old private ones only for the post owner and their writer)."""
    st = get_state(request)
    with st.database.session() as db:
        return ideas.list_comments(db, current_user(request, db), post_id, before=before, limit=limit)


@router.get("/posts/{post_id}/likers")
def post_likers(post_id: str, request: Request, cursor: str | None = Query(default=None, max_length=20)) -> dict:
    """V6: who liked (name, picture, star). Who disliked is never exposed — the count only."""
    st = get_state(request)
    with st.database.session() as db:
        return ideas.likers(db, current_user(request, db), post_id, cursor=cursor)


@router.post("/posts/{post_id}/report", status_code=201)
def report_post(post_id: str, body: ReportBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = ideas.report_post(db, st.settings, st.limiter, user, post_id, reason=body.reason, details=body.details,
                                   effects=effects)
    st.dispatch(effects)
    return result


@router.delete("/comments/{comment_id}")
def delete_comment(comment_id: str, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        ideas.delete_comment(db, current_user(request, db), comment_id)
    return {"ok": True}


@router.post("/comments/{comment_id}/report", status_code=201)
def report_comment(comment_id: str, body: ReportBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        return ideas.report_comment(db, st.settings, st.limiter, user, comment_id, reason=body.reason, details=body.details)


@router.post("/comments/{comment_id}/block")
def block_commenter(comment_id: str, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        ideas.block_commenter(db, current_user(request, db), comment_id)
    return {"ok": True}


@router.get("/profiles/{ref}")
def public_profile(ref: str, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return ideas.public_profile(db, current_user(request, db), ref)


@router.get("/profiles/{ref}/posts")
def profile_posts(ref: str, request: Request, before: str | None = Query(default=None, max_length=40),
                  limit: int = Query(default=15, ge=1, le=50)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return ideas.profile_posts(db, current_user(request, db), ref, before=before, limit=limit)

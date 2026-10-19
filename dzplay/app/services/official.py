"""V6 phase 19: posting as the official account (DALTA.BIT, gold badge).

The account has no password and never signs in. It posts only:
* from the panel («الحساب الرسمي»: text and an optional picture, with the admin's 2FA code — api/admin_official.py);
* from the owner's Telegram bot: `/post text`, or a photo with a caption → a preview with «نشر» / «إلغاء» (op:).
Its posts and pictures go through the same checks and storage as everyone's (the picture pipeline, Telegram storage),
without the daily limits (OFFICIAL_POSTS_PER_DAY, 0 = no limit). One official post can be pinned at the top of the
ideas for a chosen time (posts.pinned_until); it unpins itself. Every action is audited.
The account is outside the membership, the rewards and the ledger (ledger.post refuses it) and never receives
messages (messaging.direct_target).
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import clock
from app.config import DAY, Settings
from app.errors import AppError, not_found
from app.models import MediaItem, OfficialDraft, Post
from app.services import audit
from app.services.admin_content import official_user
from app.services.content import clean_message
from app.services.messaging import Effects, iso

log = logging.getLogger("dzplay.official")
MAX_PIN_HOURS = 24 * 30


def _today_count(db: Session, author_id: str) -> int:
    since = clock.utcnow() - timedelta(seconds=DAY)
    return db.scalar(select(func.count()).select_from(Post).where(Post.author_id == author_id, Post.created_at > since)) or 0


def create_post(db: Session, settings: Settings, *, content: object, media_id: object, actor: str,
                effects: Effects | None) -> dict:
    from app.services import ideas, media_items

    user = official_user(db)
    limit = int(settings.OFFICIAL_POSTS_PER_DAY or 0)
    if limit and _today_count(db, user.id) >= limit:
        raise AppError(429, "official_limit", "وصل الحساب الرسمي إلى حد منشوراته اليومي ({limit}).", params={"limit": limit})
    item = None
    if media_id:
        item = media_items.claim(db, user, media_id, "idea")
        text = "" if isinstance(content, str) and not content.strip() else clean_message(
            content, settings.MAX_POST_LENGTH, "allow_plain")
    else:
        text = clean_message(content, settings.MAX_POST_LENGTH, "allow_plain")
    now = clock.utcnow()
    post = Post(author_id=user.id, content=text, created_at=now, updated_at=now, media_id=item.id if item else None)
    db.add(post)
    db.flush()
    if item is not None:  # published at once (no approval queue for the official account)
        item.state, item.attached_type, item.attached_id, item.attached_at = "attached", "post", post.id, now
    audit.record(db, actor, "official_post", target_type="post", target_id=post.id,
                 detail=(text[:120] + (" [صورة]" if item else "")) or "[صورة]")
    db.flush()
    return ideas.get_post(db, user, post.id)


def _official_post(db: Session, post_id: object) -> Post:
    user = official_user(db)
    post = db.get(Post, str(post_id)[:32]) if isinstance(post_id, str) else None
    if post is None or post.author_id != user.id or post.status == "removed":
        raise not_found()
    return post


def pin(db: Session, post_id: object, hours: object, actor: str) -> dict:
    """Pin for `hours` (1..MAX_PIN_HOURS); 0 unpins. One pinned post at a time."""
    if not isinstance(hours, int) or not 0 <= hours <= MAX_PIN_HOURS:
        raise AppError(400, "invalid_input", "طلب غير صالح.")
    post = _official_post(db, post_id)
    if hours:
        db.execute(update(Post).where(Post.pinned_until.is_not(None), Post.id != post.id).values(pinned_until=None))
        post.pinned_until = clock.utcnow() + timedelta(hours=hours)
    else:
        post.pinned_until = None
    audit.record(db, actor, "official_pin" if hours else "official_unpin", target_type="post", target_id=post.id,
                 detail=f"{hours}h" if hours else None)
    db.flush()
    return {"id": post.id, "pinned_until": iso(post.pinned_until)}


def delete(db: Session, post_id: object, actor: str, effects: Effects | None) -> None:
    from app.services.media_items import remove_for_post

    post = _official_post(db, post_id)
    remove_for_post(db, post, actor, effects)
    audit.record(db, actor, "official_delete", target_type="post", target_id=post.id, detail=post.content[:120] or None)
    db.delete(post)


def pinned_post(db: Session) -> Post | None:
    """The official post pinned now (it unpins itself when its time is over)."""
    return db.scalar(select(Post).where(Post.pinned_until > clock.utcnow(), Post.status == "visible")
                     .order_by(Post.pinned_until.desc()))


def panel_view(db: Session, limit: int = 50) -> dict:
    user = official_user(db)
    rows = db.scalars(select(Post).where(Post.author_id == user.id, Post.status != "removed")
                      .order_by(Post.created_at.desc()).limit(limit)).all()
    now = clock.utcnow()
    return {"account": {"public_id": user.public_id, "posts_today": _today_count(db, user.id)},
            "posts": [{"id": p.id, "content": p.content, "created_at": iso(p.created_at), "media": bool(p.media_id),
                       "likes": p.likes_count or 0, "comments": p.comments_count or 0,
                       "pinned_until": iso(p.pinned_until) if p.pinned_until and p.pinned_until > now else None}
                      for p in rows]}


# ----------------------------------------------------------------------------------- pictures (panel + bot)


def start_picture(state, data: bytes) -> str:
    """Store the bytes for the picture pipeline (the same checks as everyone's) and return the upload id."""
    from app.services import media_items

    reason = state.pipeline.unavailable()
    if reason:
        raise AppError(503, "uploads_unavailable", reason)
    with state.database.session() as db:
        item, job = media_items.begin_official_upload(db, state.settings, len(data))
        item_id = item.id
    state.pipeline.input_path(item_id).write_bytes(data)
    state.pipeline.submit(item_id, job)
    return item_id


def picture_state(db: Session, item_id: object) -> dict:
    item = db.get(MediaItem, str(item_id)[:32]) if isinstance(item_id, str) else None
    if item is None or item.owner_id != official_user(db).id:
        raise not_found()
    from app.i18n import tr

    return {"id": item.id, "state": item.state, "error": tr(item.error) if item.error else None}


def wait_ready(state, item_id: str, timeout: float = 120.0) -> str:
    """Background (bot): wait for the pipeline. Returns the final state (ready / rejected / failed / timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with state.database.session() as db:
            item = db.get(MediaItem, item_id)
            if item is None:
                return "failed"
            if item.state not in ("uploaded", "processing"):
                return item.state
        time.sleep(0.5)
    return "timeout"


# ----------------------------------------------------------------------------------- the bot: /post


def _draft_ref(draft_id: str) -> str:
    return draft_id[:24]


def install(state) -> None:
    """/post (text) or a photo with a caption in the owner's chat → a preview with «نشر» / «إلغاء»."""
    bot = state.bot
    if bot is None:
        return

    def on_post(b, msg: dict) -> None:
        text = msg.get("caption") if msg.get("photo") or msg.get("document") else msg.get("text") or ""
        text = (text or "").strip()
        if text.split(maxsplit=1)[:1] and text.split(maxsplit=1)[0].split("@", 1)[0].lower() == "/post":
            text = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
        file_id = None
        if msg.get("photo"):
            file_id = max(msg["photo"], key=lambda p: p.get("file_size") or p.get("width") or 0).get("file_id")
        elif msg.get("document") and str(msg["document"].get("mime_type", "")).startswith("image/"):
            file_id = msg["document"].get("file_id")
        if not text and not file_id:
            b.say("اكتب: /post نص المنشور، أو أرسل صورة مع وصف.")
            return
        with state.database.session() as db:
            draft = OfficialDraft(content=text[:5000], tg_file_id=file_id, created_at=clock.utcnow())
            db.add(draft)
            db.flush()
            ref = _draft_ref(draft.id)
        preview = (text[:900] or "—") + ("\n🖼 مع صورة" if file_id else "")
        b.say(f"📝 منشور باسم الحساب الرسمي:\n\n{preview}\n\nانشره؟",
              reply_markup={"inline_keyboard": [[{"text": "✅ نشر", "callback_data": f"op:yes:{ref}"},
                                                 {"text": "✖️ إلغاء", "callback_data": f"op:no:{ref}"}]]})

    def publish(b, draft_id: str, actor: str) -> None:
        with state.database.session() as db:
            draft = db.get(OfficialDraft, draft_id)
            content, file_id = draft.content, draft.tg_file_id
        media_id = None
        try:
            if file_id:
                import tempfile
                from pathlib import Path

                with tempfile.TemporaryDirectory() as tmp:
                    dest = Path(tmp) / "photo"
                    b.tg.download(file_id, dest)
                    media_id = start_picture(state, dest.read_bytes())
                if wait_ready(state, media_id) != "ready":
                    with state.database.session() as db:
                        why = picture_state(db, media_id)["error"] or "رُفضت الصورة"
                    raise AppError(400, "media_rejected", why)
            effects = Effects()
            with state.database.session() as db:
                post = create_post(db, state.settings, content=content, media_id=media_id, actor=actor, effects=effects)
                draft = db.get(OfficialDraft, draft_id)
                draft.status, draft.post_id = "posted", post["id"]
            state.dispatch(effects)
            b.say("✅ نُشر المنشور باسم الحساب الرسمي.")
        except Exception as exc:  # noqa: BLE001 - the owner is told why
            with state.database.session() as db:
                draft = db.get(OfficialDraft, draft_id)
                if draft is not None:
                    draft.status = "failed"
            message = exc.message if isinstance(exc, AppError) else type(exc).__name__
            b.say(f"❌ لم يُنشر: {message}")

    def on_callback(b, cq: dict, payload: str) -> None:
        from app.services.media_moderation import mark_done

        action, _, ref = payload.partition(":")
        msg = cq.get("message") or {}
        with state.database.session() as db:
            draft = db.scalar(select(OfficialDraft).where(OfficialDraft.id.like(f"{ref}%"))) if ref else None
            if draft is None or draft.status != "pending" or action not in ("yes", "no") \
                    or draft.created_at < clock.utcnow() - timedelta(hours=24):
                b.tg.answer_callback(cq.get("id"), "انتهت صلاحية المسودة")
                return
            draft.status = "publishing" if action == "yes" else "cancelled"
            draft_id = draft.id
        b.tg.answer_callback(cq.get("id"), "جارٍ النشر…" if action == "yes" else "أُلغي")
        mark_done(state, (msg.get("chat") or {}).get("id"), msg.get("message_id"),
                  "⏳ جارٍ النشر" if action == "yes" else "✖️ أُلغي")
        if action == "yes":  # off the updates thread: a picture waits for its checks
            b.run_later(publish, b, draft_id, f"telegram:{(cq.get('from') or {}).get('id')}")

    bot.post_handler = on_post
    bot.callback_handlers["op"] = on_callback

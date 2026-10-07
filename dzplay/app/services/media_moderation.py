"""Moderation of user media through Telegram and the admin panel (V5).

* Every published idea picture is copied from the private storage channel to the private
  moderation group with: type, the uploader's public ID, the caption, and inline buttons
  (delete · ban the uploader · approve/reject when approval is required).
* Chat pictures reach the moderators only when they are reported.
* Reports: the item is kept as evidence (legal_hold); REPORT_AUTO_HIDE_THRESHOLD distinct reporters hide
  it until reviewed; a "minor" report hides it at once and asks the moderators to confirm
  (confirmation = removal for everyone + ban + the evidence copy is kept, never auto-deleted).
* Telegram buttons and panel buttons call the same `act()`; every action goes to the audit log.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import MediaItem, Message, Post, Report, User
from app.services import audit
from app.services.content import preview
from app.services.media_items import MEDIA_ID, discard
from app.services.messaging import Effects
from app.services.telegram import TelegramError

log = logging.getLogger("dzplay.media_moderation")

ACTIONS = {
    "ok": "✅ قُبل",
    "no": "❌ رُفض",
    "del": "🗑 حُذف",
    "ban": "⛔ حُذف وحُظر الناشر",
    "keep": "↩️ أُبقي",
    "minor": "🚨 حُذف وحُظر (قاصر) — محفوظ كدليل",
}
TITLES = {"idea": "🖼 صورة مع فكرة", "chat": "💬 صورة محادثة", "avatar": "👤 صورة شخصية"}
MODES = {
    "published": "منشورة",
    "review": "⏳ بانتظار الموافقة",
    "report": "⚠️ بلاغ",
    "minor": "🚨 بلاغ: محتوى يخص قاصرًا",
}


def moderation_chat(state) -> str:
    chat = str(state.settings.TELEGRAM_MODERATION_CHAT_ID or "").strip()
    if chat:
        return chat
    return state.bot.admin_id if state.bot is not None else ""


def _caption(db: Session, item: MediaItem, mode: str) -> str:
    lines = [f"{TITLES.get(item.purpose, item.purpose)} — {MODES.get(mode, mode)}",
             f"الناشر: {item.owner_public_id or '—'}"]
    if item.attached_type == "post":
        post = db.get(Post, item.attached_id)
        if post is not None and post.content:
            lines.append(f"النص: {preview(post.content, 600)}")
    if item.reports_count:
        lines.append(f"البلاغات: {item.reports_count}")
    if item.nsfw_score is not None:
        lines.append(f"فحص الخادم: {item.nsfw_score:.2f}")
    lines.append(f"المعرّف: {item.id[:12]}")
    return "\n".join(lines)


def buttons(item_id: str, mode: str) -> dict:
    b = lambda text, action: {"text": text, "callback_data": f"md:{action}:{item_id}"}  # noqa: E731
    rows = {
        "review": [[b("✅ قبول", "ok"), b("❌ رفض", "no")], [b("⛔ حظر الناشر", "ban")]],
        "published": [[b("🗑 حذف", "del"), b("⛔ حظر الناشر", "ban")]],
        "report": [[b("🗑 حذف", "del"), b("↩️ إبقاء", "keep")], [b("⛔ حظر الناشر", "ban")]],
        "minor": [[b("🚨 تأكيد: قاصر — حذف وحظر", "minor")], [b("↩️ ليس مخالفًا", "keep")]],
    }
    return {"inline_keyboard": rows.get(mode, rows["published"])}


def announce(state, item_id: str, mode: str) -> None:
    """Copy the stored file to the moderation chat with its context and action buttons (background)."""
    tg, chat, storage = state.telegram, moderation_chat(state), str(state.settings.TELEGRAM_STORAGE_CHANNEL_ID or "")
    if tg is None or not chat or not storage:
        return
    with state.database.session() as db:
        item = db.get(MediaItem, item_id)
        if item is None or not item.tg_message_id:
            return
        caption, source = _caption(db, item, mode), item.tg_message_id
    res = tg.copy_message(chat, storage, source, caption=caption, reply_markup=buttons(item_id, mode))
    with state.database.session() as db:
        item = db.get(MediaItem, item_id)
        if item is not None:
            item.tg_mod_message_id = res.get("message_id") if isinstance(res, dict) else None


def delete_from_storage(state, item_id: str) -> None:
    """Remove the file from the storage channel (background). Kept while the item is evidence."""
    tg, storage = state.telegram, str(state.settings.TELEGRAM_STORAGE_CHANNEL_ID or "")
    with state.database.session() as db:
        item = db.get(MediaItem, item_id)
        if item is None or item.legal_hold or not item.tg_message_id:
            return
        message_id = item.tg_message_id
    if tg is None or not storage:
        return
    try:
        tg.delete_message(storage, message_id)
    except TelegramError as exc:  # e.g. older than 48 h: Telegram no longer lets bots delete it
        # Best effort, never retried in a loop: the app has already forgotten the file (no file_id, no cache).
        log.warning("storage message %s not deleted (%s): delete it by hand in the channel if needed", message_id, exc)
    with state.database.session() as db:
        item = db.get(MediaItem, item_id)
        if item is not None:
            item.tg_message_id = None
            if not item.legal_hold:
                item.tg_file_id = None


def mark_done(state, chat_id, message_id: int, label: str) -> None:
    """Replace the buttons of a moderation message by the outcome (background)."""
    if state.telegram is None or not chat_id or not message_id:
        return
    try:
        state.telegram.edit_reply_markup(chat_id, message_id, {"inline_keyboard": [[{"text": label[:60],
                                                                                     "callback_data": "md:noop"}]]})
    except TelegramError as exc:
        log.info("moderation message not updated: %s", exc)


# ---------------------------------------------------------------------------
# actions (Telegram buttons and the admin panel)
# ---------------------------------------------------------------------------


def _target_post(db: Session, item: MediaItem) -> Post | None:
    return db.get(Post, item.attached_id) if item.attached_type == "post" and item.attached_id else None


def act(db: Session, settings: Settings, item: MediaItem, action: str, actor: str, effects: Effects) -> str:
    if action not in ACTIONS:
        raise ValueError("unknown action")
    post = _target_post(db, item)
    if action == "ok":
        item.review, item.hidden = "approved", False
        if post is not None and post.status in ("pending", "hidden"):
            post.status = "visible"
    elif action == "keep":
        item.hidden, item.legal_hold = False, False
        if post is not None and post.status == "hidden":
            post.status = "visible"
        if item.state in ("removed", "expired") and item.tg_message_id:
            effects.later(delete_from_storage, item.id)  # no longer needed as evidence
        db.execute(Report.__table__.update().where(Report.media_id == item.id, Report.status == "open")
                   .values(status="dismissed", resolution="keep"))
    else:  # del | no | ban | minor
        if action == "minor":
            item.legal_hold = True
        if action == "no":
            item.review = "rejected"
        if post is not None and post.status != "removed":
            post.status = "removed"
        if item.state not in ("removed", "expired"):
            discard(db, item, actor, "removed", effects)
        if item.attached_type == "user" and item.attached_id:  # V6: a removed profile picture
            owner = db.get(User, item.attached_id)
            if owner is not None and owner.avatar_media_id == item.id:
                owner.avatar_media_id = None
        if action in ("ban", "minor") and item.owner_id:
            owner = db.get(User, item.owner_id)
            if owner is not None and owner.status != "banned":
                from app.services.admin import set_user_status

                set_user_status(db, owner.id, "banned", effects, settings)
        db.execute(Report.__table__.update().where(Report.media_id == item.id, Report.status == "open")
                   .values(status="resolved", resolution=action))
    if item.owner_id:
        effects.signal(item.owner_id, "media")
    audit.record(db, actor, f"media_{action}", target_type="media", target_id=item.id[:12],
                 detail=f"{item.purpose} {item.owner_public_id or ''}".strip())
    db.flush()
    return ACTIONS[action]


def on_report(db: Session, settings: Settings, item: MediaItem, reason: str, effects: Effects) -> None:
    """A user reported this picture (idea or chat): keep evidence, maybe hide, tell the moderators."""
    first = not item.reports_count
    item.reports_count = (item.reports_count or 0) + 1
    item.legal_hold = True
    post = _target_post(db, item)
    reporters = db.scalar(select(func.count(func.distinct(Report.reporter_id))).where(Report.media_id == item.id)) or 0
    threshold = settings.REPORT_AUTO_HIDE_THRESHOLD
    was_hidden = bool(item.hidden)
    if reason == "minor" or (threshold > 0 and reporters >= threshold):
        item.hidden = True
        if post is not None and post.status == "visible":
            post.status = "hidden"
    if reason == "minor":
        effects.later(announce, item.id, "minor")
    elif first or (item.hidden and not was_hidden):
        effects.later(announce, item.id, "report")


# ---------------------------------------------------------------------------
# Telegram buttons
# ---------------------------------------------------------------------------


def install(state) -> None:
    """Register the md:<action>:<item id> callback on the bot (admin chat + moderation group)."""
    bot = state.bot

    def on_callback(bot_, cq: dict, payload: str) -> None:
        action, _, item_id = payload.partition(":")
        if action == "noop":
            bot_.tg.answer_callback(cq.get("id"), "")
            return
        if action not in ACTIONS or not MEDIA_ID.match(item_id):
            bot_.tg.answer_callback(cq.get("id"), "غير معروف")
            return
        who = cq.get("from") or {}
        actor = f"telegram:{who.get('id')}"
        effects = Effects()
        with state.database.session() as db:
            item = db.get(MediaItem, item_id)
            if item is None:
                bot_.tg.answer_callback(cq.get("id"), "غير موجود")
                return
            label = act(db, state.settings, item, action, actor, effects)
        state.dispatch(effects)
        bot_.tg.answer_callback(cq.get("id"), label)
        msg = cq.get("message") or {}
        by = who.get("username") or who.get("first_name") or "مشرف"
        mark_done(state, (msg.get("chat") or {}).get("id"), msg.get("message_id"), f"{label} — {by}")

    if bot is not None:
        bot.callback_handlers["md"] = on_callback


def act_from_panel(state, db: Session, item: MediaItem, action: str, actor: str, effects: Effects) -> str:
    label = act(db, state.settings, item, action, actor, effects)
    if item.tg_mod_message_id:
        effects.later(lambda st, chat, mid, text: mark_done(st, chat, mid, text), moderation_chat(state),
                      item.tg_mod_message_id, f"{label} — {actor}")
    return label


def chat_message_of(db: Session, item: MediaItem) -> Message | None:
    return db.get(Message, item.attached_id) if item.attached_type == "message" and item.attached_id else None

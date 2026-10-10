"""V5 support tickets.

* A ticket is e-mailed to SUPPORT_INBOX_EMAIL: subject "[<APP_NAME> #1024] …", Reply-To = the user's own
  e-mail (so a reply written in Gmail reaches the user directly), body with the public ID (never the
  internal id). A short notice goes to the admin's Telegram chat.
* Replies written in the panel appear in «تذاكري» and the user gets an e-mail that a reply is waiting.
* Only the owner can read or write their tickets (404 for anyone else).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.config import DAY, HOUR, Settings
from app.errors import AppError, not_found
from app.models import SupportMessage, SupportTicket, User
from app.services.content import clean_message, preview
from app.services.messaging import Effects, _check_limits, iso
from app.services.rate_limit import Limit

log = logging.getLogger("dzplay.support")

CATEGORIES = {
    "technical": "مشكلة تقنية", "account": "الحساب", "verification": "التوثيق", "payment": "الدفع",
    "report": "بلاغ", "suggestion": "اقتراح", "other": "أخرى",
}
STATUS_AR = {"open": "مفتوحة", "answered": "تم الرد", "closed": "مغلقة"}
NUMBER_BASE = 1000  # shown number = id + 1000 (#1001, #1002…)


def number(t: SupportTicket) -> int:
    return NUMBER_BASE + t.id


def _ticket_view(t: SupportTicket, last: SupportMessage | None = None) -> dict:
    return {"id": t.id, "number": number(t), "category": t.category, "category_label": CATEGORIES.get(t.category, t.category),
            "subject": t.subject, "status": t.status, "status_label": STATUS_AR.get(t.status, t.status),
            "unread": bool(t.user_unread), "created_at": iso(t.created_at), "updated_at": iso(t.updated_at),
            "last": preview(last.body, 100) if last else None}


def _message_view(m: SupportMessage) -> dict:
    return {"id": m.id, "from": "me" if m.author == "user" else "support", "body": m.body, "created_at": iso(m.created_at)}


def _own(db: Session, user: User, ticket_id: object) -> SupportTicket:
    if not isinstance(ticket_id, int):
        raise not_found()
    t = db.get(SupportTicket, ticket_id)
    if t is None or t.user_id != user.id:
        raise not_found()
    return t


def _clean_body(settings: Settings, body: object) -> str:
    return clean_message(body, settings.SUPPORT_MAX_LENGTH, "allow_plain")


def create(db: Session, settings: Settings, limiter, user: User, *, category: object, subject: object, body: object,
           effects: Effects) -> dict:
    if user.status == "banned":
        raise AppError(403, "account_banned", "تم إيقاف هذا الحساب.")
    if category not in CATEGORIES:
        raise AppError(400, "invalid_category", "اختر نوع المشكلة.")
    subj = clean_message(subject, 120, "allow_plain") if isinstance(subject, str) and subject.strip() else CATEGORIES[category]
    text = _clean_body(settings, body)
    _check_limits(limiter, [Limit(f"support_day:{user.id}", settings.SUPPORT_TICKETS_PER_DAY, DAY),
                            Limit(f"support_hour:{user.id}", settings.SUPPORT_MESSAGES_PER_HOUR, HOUR)])
    now = clock.utcnow()
    t = SupportTicket(user_id=user.id, category=category, subject=subj, status="open", created_at=now, updated_at=now)
    db.add(t)
    db.flush()
    db.add(SupportMessage(ticket_id=t.id, author="user", body=text, created_at=now))
    db.flush()
    effects.later(notify_new, t.id, True)
    return {"ticket": _ticket_view(t)}


def add_message(db: Session, settings: Settings, limiter, user: User, ticket_id: object, body: object,
                effects: Effects) -> dict:
    t = _own(db, user, ticket_id)
    if t.status == "closed":
        raise AppError(409, "ticket_closed", "هذه التذكرة مغلقة. افتح تذكرة جديدة إذا احتجت.")
    text = _clean_body(settings, body)
    _check_limits(limiter, [Limit(f"support_hour:{user.id}", settings.SUPPORT_MESSAGES_PER_HOUR, HOUR)])
    now = clock.utcnow()
    m = SupportMessage(ticket_id=t.id, author="user", body=text, created_at=now)
    db.add(m)
    t.status, t.updated_at = "open", now
    db.flush()
    effects.later(notify_new, t.id, False)
    return {"message": _message_view(m), "ticket": _ticket_view(t)}


def close(db: Session, user: User, ticket_id: object) -> dict:
    t = _own(db, user, ticket_id)
    t.status, t.updated_at = "closed", clock.utcnow()
    return {"ticket": _ticket_view(t)}


def list_mine(db: Session, user: User) -> dict:
    tickets = db.execute(select(SupportTicket).where(SupportTicket.user_id == user.id)
                         .order_by(SupportTicket.updated_at.desc()).limit(100)).scalars().all()
    return {"tickets": [_ticket_view(t) for t in tickets], "categories": CATEGORIES}


def get_mine(db: Session, user: User, ticket_id: object) -> dict:
    t = _own(db, user, ticket_id)
    t.user_unread = False
    msgs = db.execute(select(SupportMessage).where(SupportMessage.ticket_id == t.id)
                      .order_by(SupportMessage.created_at, SupportMessage.id)).scalars().all()
    return {"ticket": _ticket_view(t), "messages": [_message_view(m) for m in msgs]}


def unread_count(db: Session, user_id: str) -> int:
    return db.scalar(select(func.count()).select_from(SupportTicket).where(
        SupportTicket.user_id == user_id, SupportTicket.user_unread.is_(True))) or 0


# ---------------------------------------------------------------------------
# admin (panel)
# ---------------------------------------------------------------------------


def admin_list(db: Session, status: str = "", q: str = "", limit: int = 100) -> dict:
    stmt = select(SupportTicket, User).join(User, User.id == SupportTicket.user_id).order_by(SupportTicket.updated_at.desc())
    if status in STATUS_AR:
        stmt = stmt.where(SupportTicket.status == status)
    if q:
        num = q.lstrip("#")
        if num.isdigit() and int(num) > NUMBER_BASE:
            stmt = stmt.where(SupportTicket.id == int(num) - NUMBER_BASE)
        else:
            stmt = stmt.where(User.public_id == q.upper())
    rows = db.execute(stmt.limit(min(limit, 300))).all()
    return {"tickets": [{**_ticket_view(t), "public_id": u.public_id, "user_ref": u.id} for t, u in rows],
            "counts": {s: db.scalar(select(func.count()).select_from(SupportTicket).where(SupportTicket.status == s)) or 0
                       for s in STATUS_AR}}


def admin_get(db: Session, ticket_id: int) -> dict:
    t = db.get(SupportTicket, ticket_id)
    if t is None:
        raise not_found()
    u = db.get(User, t.user_id)
    msgs = db.execute(select(SupportMessage).where(SupportMessage.ticket_id == t.id)
                      .order_by(SupportMessage.created_at, SupportMessage.id)).scalars().all()
    return {"ticket": {**_ticket_view(t), "public_id": u.public_id if u else None, "user_ref": t.user_id,
                       "email": u.email if u else None},
            "messages": [{**_message_view(m), "from": m.author, "admin": m.admin} for m in msgs]}


def admin_reply(db: Session, settings: Settings, ticket_id: int, body: object, admin: str, effects: Effects) -> dict:
    t = db.get(SupportTicket, ticket_id)
    if t is None:
        raise not_found()
    text = _clean_body(settings, body)
    now = clock.utcnow()
    db.add(SupportMessage(ticket_id=t.id, author="support", admin=admin, body=text, created_at=now))
    t.status, t.updated_at, t.user_unread = "answered", now, True
    db.flush()
    effects.signal(t.user_id, "support")
    effects.later(notify_user_reply, t.id)
    return admin_get(db, ticket_id)


def admin_set_status(db: Session, ticket_id: int, status: str) -> dict:
    t = db.get(SupportTicket, ticket_id)
    if t is None:
        raise not_found()
    if status not in STATUS_AR:
        raise AppError(400, "invalid_status", "حالة غير صالحة.")
    t.status, t.updated_at = status, clock.utcnow()
    return admin_get(db, ticket_id)


# ---------------------------------------------------------------------------
# notifications (background, after the commit)
# ---------------------------------------------------------------------------


def notify_new(state, ticket_id: int, first: bool) -> None:
    """V6 phase 7: from the support mailbox to the support inbox (SUPPORT_INBOX_EMAIL, else the official
    support address); Reply-To = the user, so answering the e-mail reaches them directly."""
    from app.services import mail

    with state.database.session() as db:
        t = db.get(SupportTicket, ticket_id)
        u = db.get(User, t.user_id) if t else None
        if t is None or u is None:
            return
        last = db.execute(select(SupportMessage).where(SupportMessage.ticket_id == t.id)
                          .order_by(SupportMessage.id.desc()).limit(1)).scalar()
        num, cat, subj, pid, body = (number(t), CATEGORIES.get(t.category, t.category), t.subject, u.public_id,
                                     last.body if last else "")
        inbox = state.settings.SUPPORT_INBOX_EMAIL.strip() or mail.support_address(db, state.settings)
        out = mail.prepare(db, state.settings, "support", inbox, "email.ticket_new",
                           {"ticket": num, "subject": subj, "kind": "تذكرة جديدة" if first else "رد جديد من المستخدم",
                            "category": cat, "public_id": pid, "message": body}, reply_to=u.email) if inbox else None
    if out is not None:
        try:
            mail.deliver(out)
        except Exception as exc:  # noqa: BLE001 - mail is best effort; the ticket is saved anyway
            log.warning("support mail not sent: %s", type(exc).__name__)
    if state.bot is not None:
        state.bot.say(f"🎫 {'تذكرة جديدة' if first else 'رد على تذكرة'} #{num} — {cat}\n{pid}\n{subj}\n\n{preview(body, 300)}")


def notify_user_reply(state, ticket_id: int) -> None:
    """V6 phase 7: from the support mailbox to the user; Reply-To = the official support address."""
    from app.services import mail

    with state.database.session() as db:
        t = db.get(SupportTicket, ticket_id)
        u = db.get(User, t.user_id) if t else None
        if t is None or u is None or not u.email or u.is_system or u.is_official:
            return
        out = mail.prepare(db, state.settings, "support", u.email, "email.ticket_reply",
                           {"ticket": number(t), "subject": t.subject})
    if out is None:
        return
    try:
        mail.deliver(out)
    except Exception as exc:  # noqa: BLE001
        log.warning("support reply mail not sent: %s", type(exc).__name__)

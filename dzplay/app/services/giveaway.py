"""V6 phase 5c: «الظرف الأحمر» — a giveaway round. Members of the community enter with an e-mail address they
confirm by code (the account e-mail is not confirmed at registration, so it is checked too, once). After the end
time the admin draws the winners at random (secrets.SystemRandom) among confirmed, active, unflagged entries; the
draw is logged (SHA-256 of the sorted entrants, the picks, the time, the admin). Prize codes are typed in the panel,
stored sealed (SECRET_KEY), shown to admins only, and e-mailed to the winners with an in-app notification.

Entering never depends on membership or payment.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError, not_found
from app.models import GiveawayEntry, GiveawayRound, GiveawayWinner, User
from app.security.crypto import seal, unseal
from app.services import audit, email_codes, notify
from app.services.messaging import Effects, iso

log = logging.getLogger("dzplay.giveaway")
_PURPOSE = "giveaway"
STATUS_AR = {"open": "مفتوحة", "drawn": "تم السحب", "sent": "أُرسلت الجوائز", "cancelled": "ملغاة"}


def _round(db: Session, round_id: object) -> GiveawayRound:
    r = db.get(GiveawayRound, round_id) if isinstance(round_id, str) and len(round_id) <= 32 else None
    if r is None:
        raise not_found()
    return r


def current(db: Session) -> GiveawayRound | None:
    return db.scalar(select(GiveawayRound).where(GiveawayRound.status != "cancelled")
                     .order_by(GiveawayRound.created_at.desc()))


def _winner_names(db: Session, r: GiveawayRound) -> list[str]:
    from app.services import names

    rows = db.execute(select(User).join(GiveawayEntry, GiveawayEntry.user_id == User.id)
                      .join(GiveawayWinner, GiveawayWinner.entry_id == GiveawayEntry.id)
                      .where(GiveawayWinner.round_id == r.id)).scalars().all()
    return [names.shown_name(u) for u in rows]


def view(db: Session, user: User) -> dict:
    r = current(db)
    out = {"round": None, "entry": None, "account_email": email_codes.mask(user.email or ""),
           "account_email_verified": user.email_verified_at is not None}
    if r is None:
        return out
    entries = db.scalar(select(func.count()).select_from(GiveawayEntry).where(GiveawayEntry.round_id == r.id,
                                                                              GiveawayEntry.email_verified_at.is_not(None))) or 0
    won = db.scalar(select(GiveawayWinner.id).join(GiveawayEntry, GiveawayEntry.id == GiveawayWinner.entry_id)
                    .where(GiveawayWinner.round_id == r.id, GiveawayEntry.user_id == user.id)) is not None
    out["round"] = {"id": r.id, "title": r.title, "description": r.description, "winners_count": r.winners_count,
                    "ends_at": iso(r.ends_at), "status": r.status, "open": _is_open(r), "entries": entries,
                    "winners": _winner_names(db, r) if r.show_winners and r.status in ("drawn", "sent") else None,
                    "i_won": won if r.status == "sent" else None}
    e = db.scalar(select(GiveawayEntry).where(GiveawayEntry.round_id == r.id, GiveawayEntry.user_id == user.id))
    if e is not None:
        out["entry"] = {"email": email_codes.mask(e.email), "confirmed": e.email_verified_at is not None}
    return out


def _is_open(r: GiveawayRound) -> bool:
    return r.status == "open" and clock.utcnow() < r.ends_at


def enter(db: Session, settings: Settings, limiter, user: User, round_id: object, email: object) -> dict:
    """Join with the account e-mail (default) or another one; a code confirms it (skipped for an account e-mail
    already confirmed once)."""
    from app.services.auth import normalize_email
    from app.services.messaging import _require_can_send

    _require_can_send(user)
    r = _round(db, round_id)
    if not _is_open(r):
        raise AppError(409, "giveaway_closed", "انتهت المشاركة في هذه الجولة.")
    addr = normalize_email(email) if isinstance(email, str) and email.strip() else (user.email or "")
    entry = db.scalar(select(GiveawayEntry).where(GiveawayEntry.round_id == r.id, GiveawayEntry.user_id == user.id))
    if entry is not None and entry.email_verified_at is not None:
        raise AppError(409, "already_entered", "أنت مشارك بالفعل في هذه الجولة.")
    taken = db.scalar(select(GiveawayEntry.id).where(GiveawayEntry.round_id == r.id, GiveawayEntry.email == addr,
                                                     GiveawayEntry.user_id != user.id))
    if taken:
        raise AppError(409, "email_taken", "هذا البريد مستعمل في مشاركة أخرى.")
    if entry is None:
        entry = GiveawayEntry(round_id=r.id, user_id=user.id, email=addr, created_at=clock.utcnow())
        db.add(entry)
    else:
        entry.email = addr
    try:
        db.flush()
    except IntegrityError:
        raise AppError(409, "already_entered", "أنت مشارك بالفعل في هذه الجولة.") from None
    if addr == (user.email or "") and user.email_verified_at is not None:
        entry.email_verified_at = clock.utcnow()
        return {"confirmed": True}
    sent = email_codes.send(db, settings, limiter, user, "giveaway", addr)
    return {"confirmed": False, **sent}


def confirm(db: Session, settings: Settings, user: User, round_id: object, code: object) -> dict:
    r = _round(db, round_id)
    if not _is_open(r):
        raise AppError(409, "giveaway_closed", "انتهت المشاركة في هذه الجولة.")
    entry = db.scalar(select(GiveawayEntry).where(GiveawayEntry.round_id == r.id, GiveawayEntry.user_id == user.id))
    if entry is None:
        raise not_found()
    email_codes.verify(db, settings, user, "giveaway", code, email=entry.email)
    now = clock.utcnow()
    entry.email_verified_at = now
    if entry.email == (user.email or "") and user.email_verified_at is None:
        user.email_verified_at = now  # the account e-mail is now confirmed: not asked again
    return {"confirmed": True}


def set_network(db: Session, user: User, round_id: str, ip_hash: str | None) -> None:
    """Called by the API after an entry: same network as another entrant of the round → flagged (excluded)."""
    entry = db.scalar(select(GiveawayEntry).where(GiveawayEntry.round_id == round_id, GiveawayEntry.user_id == user.id))
    if entry is None or not ip_hash:
        return
    entry.ip_hash = ip_hash
    other = db.scalar(select(GiveawayEntry.id).where(GiveawayEntry.round_id == round_id, GiveawayEntry.ip_hash == ip_hash,
                                                     GiveawayEntry.id != entry.id))
    if other:
        entry.flags = "dup_network"


# ---------------------------------------------------------------------------
# panel
# ---------------------------------------------------------------------------


def create_round(db: Session, body: dict, actor: str) -> dict:
    title = str(body.get("title") or "").strip()[:120]
    if len(title) < 3:
        raise AppError(400, "invalid_title", "اكتب عنوان الجولة.")
    try:
        ends = datetime.fromisoformat(str(body.get("ends_at")).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise AppError(400, "invalid_date", "تاريخ الانتهاء غير صالح.") from None
    if ends <= clock.utcnow():
        raise AppError(400, "invalid_date", "تاريخ الانتهاء يجب أن يكون في المستقبل.")
    n = body.get("winners_count")
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 1000:
        raise AppError(400, "invalid_value", "عدد الفائزين بين 1 و1000.")
    r = GiveawayRound(title=title, description=str(body.get("description") or "").strip()[:1000] or None, winners_count=n,
                      ends_at=ends, status="open", show_winners=bool(body.get("show_winners")), created_by=actor[:80],
                      created_at=clock.utcnow())
    db.add(r)
    db.flush()
    audit.record(db, actor, "giveaway_create", target_type="giveaway", target_id=r.id, detail=f"{title} · {n}")
    return admin_view(db, r)


def pick(entry_ids: list[str], k: int, rng=None) -> list[str]:
    """A fair draw: k distinct entries, each equally likely (cryptographic randomness by default)."""
    rng = rng or secrets.SystemRandom()
    return rng.sample(sorted(entry_ids), min(k, len(entry_ids)))


def draw(db: Session, r: GiveawayRound, actor: str) -> dict:
    if r.status != "open":
        raise AppError(409, "already_drawn", "تم السحب في هذه الجولة.")
    if clock.utcnow() < r.ends_at:
        raise AppError(409, "not_ended", "لم تنته مدة المشاركة بعد.")
    rows = db.execute(select(GiveawayEntry.id).join(User, User.id == GiveawayEntry.user_id).where(
        GiveawayEntry.round_id == r.id, GiveawayEntry.email_verified_at.is_not(None), GiveawayEntry.flags.is_(None),
        User.status == "active")).scalars().all()
    entrants = sorted(rows)
    chosen = pick(entrants, r.winners_count)
    now = clock.utcnow()
    for eid in chosen:
        db.add(GiveawayWinner(round_id=r.id, entry_id=eid))
    r.status, r.drawn_at = "drawn", now
    r.draw_log = json.dumps({"entrants": len(entrants), "entrants_sha256": hashlib.sha256("\n".join(entrants).encode()).hexdigest(),
                             "winners": chosen, "at": iso(now), "by": actor, "method": "secrets.SystemRandom().sample"})
    audit.record(db, actor, "giveaway_draw", target_type="giveaway", target_id=r.id,
                 detail=f"{len(chosen)}/{len(entrants)} sha256={json.loads(r.draw_log)['entrants_sha256'][:16]}")
    db.flush()
    return admin_view(db, r)


def set_codes(db: Session, settings: Settings, r: GiveawayRound, codes: object, actor: str) -> dict:
    if r.status != "drawn":
        raise AppError(409, "not_drawn", "اسحب الفائزين أولًا.")
    winners = db.execute(select(GiveawayWinner).where(GiveawayWinner.round_id == r.id).order_by(GiveawayWinner.id)).scalars().all()
    if not isinstance(codes, list) or not codes or any(not isinstance(c, str) or not c.strip() or len(c) > 300 for c in codes):
        raise AppError(400, "invalid_codes", "أدخل رمزًا لكل فائز، أو رمزًا واحدًا للجميع.")
    if len(codes) not in (1, len(winners)):
        raise AppError(400, "invalid_codes", f"أدخل {len(winners)} رموز (رمز لكل فائز) أو رمزًا واحدًا.")
    for i, w in enumerate(winners):
        w.code_sealed = seal(settings.SECRET_KEY, _PURPOSE, (codes[i] if len(codes) > 1 else codes[0]).strip())
    audit.record(db, actor, "giveaway_codes", target_type="giveaway", target_id=r.id, detail=f"{len(winners)} winners")
    return admin_view(db, r, settings)


def send(db: Session, settings: Settings, r: GiveawayRound, actor: str, effects: Effects) -> dict:
    if r.status != "drawn":
        raise AppError(409, "not_drawn", "لا يمكن الإرسال الآن.")
    winners = db.execute(select(GiveawayWinner).where(GiveawayWinner.round_id == r.id)).scalars().all()
    if any(not w.code_sealed for w in winners):
        raise AppError(400, "codes_missing", "أدخل رموز الجوائز أولًا.")
    for w in winners:
        entry = db.get(GiveawayEntry, w.entry_id)
        if entry is None:
            continue
        effects.later(mail_code, w.id)
        notify.create(db, entry.user_id, "giveaway_won", effects, data={"text": f"🧧 ربحت في «{r.title}»! تحقق من بريدك."})
    r.status, r.sent_at = "sent", clock.utcnow()
    audit.record(db, actor, "giveaway_send", target_type="giveaway", target_id=r.id, detail=f"{len(winners)} e-mails")
    return admin_view(db, r, settings)


def cancel(db: Session, r: GiveawayRound, actor: str) -> dict:
    if r.status in ("sent", "cancelled"):
        raise AppError(409, "not_allowed", "لا يمكن إلغاء هذه الجولة.")
    r.status = "cancelled"
    audit.record(db, actor, "giveaway_cancel", target_type="giveaway", target_id=r.id)
    return admin_view(db, r)


def mail_code(state, winner_id: int) -> None:
    from app.services import mailer, runtime_config

    with state.database.session() as db:
        w = db.get(GiveawayWinner, winner_id)
        entry = db.get(GiveawayEntry, w.entry_id) if w else None
        r = db.get(GiveawayRound, w.round_id) if w else None
        if w is None or entry is None or r is None or not w.code_sealed:
            return
        settings = runtime_config.effective_settings(db, state.settings)
        code = unseal(state.settings.SECRET_KEY, _PURPOSE, w.code_sealed)
        to, title = entry.email, r.title
    if not code or not settings.smtp_enabled:
        log.warning("giveaway code not mailed (mail off or code unreadable)")
        return
    text = (f"مبروك! 🧧\n\nربحت في «{title}». رمز جائزتك:\n\n{code}\n\n"
            f"لا تشاركه مع أحد. فريق {settings.APP_NAME} لن يطلبه منك أبدًا.\n\nفريق {settings.APP_NAME}")
    try:
        mailer.send_text(settings, to, f"{settings.APP_NAME}: جائزتك في الظرف الأحمر", text)
        with state.database.session() as db:
            w = db.get(GiveawayWinner, winner_id)
            if w is not None:
                w.sent_at = clock.utcnow()
    except Exception as exc:  # noqa: BLE001
        log.warning("giveaway mail failed: %s", type(exc).__name__)


def admin_view(db: Session, r: GiveawayRound, settings: Settings | None = None, details: bool = False) -> dict:
    counts = dict(db.execute(select(GiveawayEntry.email_verified_at.is_not(None), func.count()).where(
        GiveawayEntry.round_id == r.id).group_by(GiveawayEntry.email_verified_at.is_not(None))).all())
    out = {"id": r.id, "title": r.title, "description": r.description, "winners_count": r.winners_count,
           "ends_at": iso(r.ends_at), "status": r.status, "status_label": STATUS_AR.get(r.status),
           "show_winners": bool(r.show_winners), "entries": sum(counts.values()), "confirmed": counts.get(True, 0),
           "draw_log": json.loads(r.draw_log) if r.draw_log else None, "created_at": iso(r.created_at)}
    if details:
        entries = db.execute(select(GiveawayEntry, User).join(User, User.id == GiveawayEntry.user_id)
                             .where(GiveawayEntry.round_id == r.id).order_by(GiveawayEntry.created_at)).all()
        out["entrants"] = [{"id": e.id, "public_id": u.public_id, "name": u.display_name, "user_ref": u.id, "email": e.email,
                            "confirmed": e.email_verified_at is not None, "flags": e.flags, "status": u.status}
                           for e, u in entries]
        wins = db.execute(select(GiveawayWinner).where(GiveawayWinner.round_id == r.id).order_by(GiveawayWinner.id)).scalars().all()
        by_entry = {e["id"]: e for e in out["entrants"]}
        out["winners"] = [{"id": w.id, "entry": by_entry.get(w.entry_id), "sent_at": iso(w.sent_at),
                           "code": unseal(settings.SECRET_KEY, _PURPOSE, w.code_sealed) if settings and w.code_sealed else None,
                           "has_code": bool(w.code_sealed)} for w in wins]
    return out


def admin_list(db: Session) -> dict:
    return {"rounds": [admin_view(db, r) for r in db.execute(select(GiveawayRound).order_by(GiveawayRound.created_at.desc())
                                                             .limit(50)).scalars()]}

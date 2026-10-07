"""V6 phase 5b: promotional rewards («أرباحي») — the "rewards" ledger account, fully separate from membership.

* Referral: one level only. A link /r/<code> leaves a cookie; a new account made with it is linked to the inviter.
  When the INVITEE's membership is accepted, the inviter gets REFERRAL_REWARD, held for REFERRAL_HOLD_DAYS
  (always longer than the refund window, enforced in tunables.save). A refund within the window reverses it
  (referral_rev). Suspicious links (same network or device as the inviter, bursts from one network) wait for the
  admin instead of paying automatically.
* Group rewards (panel): activity criteria only — never membership or payment — preview first, then a fresh 2FA
  code; one entry per person and batch (idempotent).
* Individual rewards / corrections (panel): mandatory reason, audited; a deduction never exceeds what is available.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError, not_found
from app.models import LedgerEntry, Post, Referral, RewardBatch, User
from app.services import audit, ledger, notify
from app.services.messaging import Effects, iso

CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_RE = re.compile(r"^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{8}$")
REF_COOKIE = "dz_ref"
REVIEW_FLAGS = {"same_network", "burst"}  # these send the reward to the admin instead of paying it
KIND_AR = {"referral": "مكافأة دعوة", "referral_rev": "إلغاء مكافأة دعوة (استرجاع العضوية)", "contest": "مسابقة",
           "group": "مكافأة جماعية", "activity": "مكافأة نشاط", "correction": "تصحيح", "withdraw": "سحب",
           "withdraw_rev": "إرجاع سحب مرفوض"}
REF_STATUS_AR = {"joined": "سجّل", "review": "قيد المراجعة", "rewarded": "مُكافأ", "rejected": "مرفوض", "reversed": "أُلغي"}


def device_hash(settings: Settings, user_agent: str | None, language: str | None) -> str:
    raw = f"{(user_agent or '')[:300]}|{(language or '')[:60]}"
    return hashlib.sha256((settings.SECRET_KEY + "|device|" + raw).encode()).hexdigest()[:32]


def referral_code(db: Session, user: User) -> str:
    """The user's own code (created on first use)."""
    if user.referral_code:
        return user.referral_code
    for _ in range(20):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        if not db.scalar(select(User.id).where(User.referral_code == code)):
            user.referral_code = code
            db.flush()
            return code
    raise AppError(503, "try_again", "حاول مرة أخرى.")


def attach_referral(db: Session, settings: Settings, user: User, code: object, ip_hash: str | None, device: str | None) -> None:
    """A new account made through an invitation link (one level; no self-invitation; at most once)."""
    if not settings.REFERRAL_ENABLED or not isinstance(code, str) or not CODE_RE.match(code.upper()):
        return
    if user.created_at < clock.utcnow() - timedelta(minutes=10):  # existing accounts cannot be "invited" later
        return
    if db.scalar(select(Referral.id).where(Referral.referee_id == user.id)):
        return
    inviter = db.scalar(select(User).where(User.referral_code == code.upper()))
    if inviter is None or inviter.id == user.id or inviter.status != "active" or inviter.is_system or inviter.is_official:
        return
    flags = []
    if ip_hash and inviter.registration_ip_hash == ip_hash:
        flags.append("same_network")
    if device and inviter.device_hash == device:
        flags.append("same_device")  # informational: many phones share a browser signature, so it alone never blocks
    since = clock.utcnow() - timedelta(days=1)
    burst = db.scalar(select(func.count()).select_from(Referral).where(
        Referral.referrer_id == inviter.id, Referral.ip_hash == ip_hash, Referral.created_at > since)) or 0
    if ip_hash and burst >= settings.REFERRAL_FLAG_PER_DAY:
        flags.append("burst")
    db.add(Referral(referrer_id=inviter.id, referee_id=user.id, status="joined", flags=",".join(flags) or None,
                    ip_hash=ip_hash, created_at=clock.utcnow()))
    db.flush()


def _reward_referral(db: Session, settings: Settings, ref: Referral, actor: str, effects: Effects) -> None:
    amount = ledger.to_minor(settings.REFERRAL_REWARD)
    if amount <= 0:
        return
    ledger.lock_user(db, ref.referrer_id)
    ledger.post(db, user_id=ref.referrer_id, account="rewards", kind="referral", amount_minor=amount,
                source_type="referral", source_id=ref.id, actor=actor,
                available_at=clock.utcnow() + timedelta(days=settings.REFERRAL_HOLD_DAYS), note="دعوة صديق أصبح عضوًا")
    ref.status, ref.rewarded_at = "rewarded", clock.utcnow()
    notify.create(db, ref.referrer_id, "referral_reward", effects,
                  data={"text": f"مكافأة دعوة: {ledger.from_minor(amount)} USDT (معلّقة {settings.REFERRAL_HOLD_DAYS} يومًا)"})


def on_membership_accepted(db: Session, settings: Settings, user: User, req, effects: Effects) -> None:
    ref = db.scalar(select(Referral).where(Referral.referee_id == user.id))
    if ref is None or ref.status != "joined" or not settings.REFERRAL_ENABLED:
        return
    if REVIEW_FLAGS & set((ref.flags or "").split(",")):
        ref.status = "review"  # the admin approves or rejects (panel)
        return
    _reward_referral(db, settings, ref, "system:referral", effects)


def on_membership_refunded(db: Session, settings: Settings, user: User, refund, effects: Effects) -> None:
    ref = db.scalar(select(Referral).where(Referral.referee_id == user.id))
    if ref is None or ref.status not in ("rewarded", "review", "joined"):
        return
    if ref.status == "rewarded":
        paid = db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
            LedgerEntry.account == "rewards", LedgerEntry.source_type == "referral",
            LedgerEntry.source_id == ref.id)) or 0
        if paid > 0:
            ledger.lock_user(db, ref.referrer_id)
            ledger.post(db, user_id=ref.referrer_id, account="rewards", kind="referral_rev", amount_minor=-int(paid),
                        source_type="referral", source_id=ref.id, actor="system:refund", note="استُرجعت عضوية المدعو")
            notify.create(db, ref.referrer_id, "referral_reversed", effects,
                          data={"text": "أُلغيت مكافأة دعوة لأن صديقك استرجع عضويته."})
    ref.status = "reversed"


def review_referral(db: Session, settings: Settings, ref_id: object, action: str, actor: str, effects: Effects) -> dict:
    ref = db.get(Referral, ref_id) if isinstance(ref_id, str) and len(ref_id) <= 32 else None
    if ref is None:
        raise not_found()
    if ref.status != "review" or action not in ("approve", "reject"):
        raise AppError(409, "already_decided", "تم البت في هذه الدعوة.")
    if action == "approve":
        _reward_referral(db, settings, ref, actor, effects)
    else:
        ref.status = "rejected"
    audit.record(db, actor, f"referral_{action}", target_type="referral", target_id=ref.id, detail=ref.flags or "")
    db.flush()
    return referral_admin_view(db, ref)


# ---------------------------------------------------------------------------
# «أرباحي» and «دعوة الأصدقاء» (owner only)
# ---------------------------------------------------------------------------


def overview(db: Session, settings: Settings, user: User) -> dict:
    from app.services import withdrawals

    bal = ledger.balances(db, user.id, "rewards")
    entries = [{**e, "label": KIND_AR.get(e["kind"], e["kind"])} for e in ledger.entries(db, user.id, "rewards")]
    return {"balances": {k: ledger.from_minor(v) for k, v in bal.items()}, "currency": ledger.CURRENCY,
            "entries": entries, "withdraw": withdrawals.settings_view(db, settings, user),
            "note": "مكافآت ترويجية مستقلة تمامًا عن العضوية، ولا علاقة لها بالتداول."}


def referrals_view(db: Session, settings: Settings, user: User) -> dict:
    code = referral_code(db, user)
    rows = db.execute(select(Referral.status, func.count()).where(Referral.referrer_id == user.id)
                      .group_by(Referral.status)).all()
    counts = {s: n for s, n in rows}
    members = db.scalar(select(func.count()).select_from(Referral).join(User, User.id == Referral.referee_id).where(
        Referral.referrer_id == user.id, User.member_since.is_not(None), User.member_ended_at.is_(None))) or 0
    earned = db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
        LedgerEntry.user_id == user.id, LedgerEntry.account == "rewards",
        LedgerEntry.kind.in_(("referral", "referral_rev")))) or 0
    base = (settings.PUBLIC_URL or "").rstrip("/")
    return {"enabled": settings.REFERRAL_ENABLED, "code": code, "link": f"{base}/r/{code}" if base else f"/r/{code}",
            "invited": sum(counts.values()), "members": members, "earned": ledger.from_minor(int(earned)),
            "reward": ledger.from_minor(ledger.to_minor(settings.REFERRAL_REWARD)), "hold_days": settings.REFERRAL_HOLD_DAYS}


# ---------------------------------------------------------------------------
# panel: group + individual rewards, referral review
# ---------------------------------------------------------------------------


def _criteria(body: dict) -> dict:
    def num(key, lo, hi, default=0):
        v = body.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise AppError(400, "invalid_value", "قيمة غير صالحة.")
        return int(v)

    return {"active_days": num("active_days", 0, 3650), "min_posts": num("min_posts", 0, 100000),
            "min_age_days": num("min_age_days", 0, 3650)}


def _amount(value: object, *, allow_negative: bool = False) -> int:
    try:
        minor = ledger.to_minor(value if isinstance(value, (int, float, str)) and not isinstance(value, bool) else "x")
    except Exception:  # noqa: BLE001
        raise AppError(400, "invalid_amount", "مبلغ غير صالح.") from None
    if minor == 0 or abs(minor) > 100_000_00 or (minor < 0 and not allow_negative):
        raise AppError(400, "invalid_amount", "مبلغ غير صالح.")
    return minor


def _eligible(db: Session, crit: dict):
    """Activity only: recent activity, ideas published, account age. Never membership or any payment."""
    now = clock.utcnow()
    q = select(User.id).where(User.status == "active", User.is_system.is_not(True), User.is_official.is_not(True),
                              User.display_name.is_not(None))
    if crit["active_days"]:
        q = q.where(User.last_active_at >= now - timedelta(days=crit["active_days"]))
    if crit["min_age_days"]:
        q = q.where(User.created_at <= now - timedelta(days=crit["min_age_days"]))
    if crit["min_posts"]:
        posts = (select(func.count()).select_from(Post).where(Post.author_id == User.id, Post.status == "visible")
                 .scalar_subquery())
        q = q.where(posts >= crit["min_posts"])
    return q


def group_preview(db: Session, body: dict) -> dict:
    crit = _criteria(body)
    amount = _amount(body.get("amount"))
    count = db.scalar(select(func.count()).select_from(_eligible(db, crit).subquery())) or 0
    return {"count": count, "amount": ledger.from_minor(amount), "total": ledger.from_minor(amount * count), "criteria": crit}


def group_run(db: Session, body: dict, actor: str, effects: Effects) -> dict:
    """Create the batch and credit everyone (in chunks; idempotent per person and batch)."""
    crit = _criteria(body)
    amount = _amount(body.get("amount"))
    note = str(body.get("note") or "").strip()[:200]
    if len(note) < 3:
        raise AppError(400, "reason_required", "اكتب سبب المكافأة (يراه المستفيدون).")
    batch = RewardBatch(amount_minor=amount, criteria=json.dumps(crit), note=note, status="running", created_by=actor[:80],
                        created_at=clock.utcnow())
    db.add(batch)
    db.flush()
    ids = list(db.execute(_eligible(db, crit)).scalars())
    for i in range(0, len(ids), 200):
        for uid in ids[i:i + 200]:
            _entry, new = ledger.post(db, user_id=uid, account="rewards", kind="group", amount_minor=amount,
                                      source_type="group", source_id=f"{batch.id}:{uid}", actor=actor, note=note)
            if new:
                notify.create(db, uid, "reward", effects, data={"text": f"مكافأة: {ledger.from_minor(amount)} USDT — {note}"})
        db.flush()
    batch.status, batch.count, batch.total_minor, batch.done_at = "done", len(ids), amount * len(ids), clock.utcnow()
    audit.record(db, actor, "reward_group", target_type="reward_batch", target_id=batch.id,
                 detail=f"{len(ids)} × {ledger.from_minor(amount)} USDT {json.dumps(crit)}", reason=note)
    return {"batch": batch.id, "count": len(ids), "total": ledger.from_minor(batch.total_minor)}


def individual(db: Session, user: User, body: dict, actor: str, effects: Effects) -> dict:
    kind = body.get("kind")
    if kind not in ("contest", "activity", "correction"):
        raise AppError(400, "invalid_kind", "اختر نوع المكافأة.")
    amount = _amount(body.get("amount"), allow_negative=kind == "correction")
    reason = str(body.get("reason") or "").strip()[:300]
    if len(reason) < 3:
        raise AppError(400, "reason_required", "اكتب السبب.")
    ledger.lock_user(db, user.id)
    if amount < 0 and -amount > ledger.balances(db, user.id, "rewards")["available"]:
        raise AppError(400, "over_balance", "الخصم أكبر من الرصيد المتاح.")
    entry, _ = ledger.post(db, user_id=user.id, account="rewards", kind=kind, amount_minor=amount, source_type="manual",
                           source_id=secrets.token_hex(8), actor=actor, note=reason)
    audit.record(db, actor, f"reward_{kind}", target_type="user", target_id=user.public_id,
                 detail=f"{ledger.from_minor(amount)} USDT", reason=reason)
    notify.create(db, user.id, "reward", effects,
                  data={"text": f"{'مكافأة' if amount > 0 else 'تصحيح في رصيدك'}: {ledger.from_minor(amount)} USDT — {reason}"})
    return {"id": entry.id, "balances": {k: ledger.from_minor(v) for k, v in ledger.balances(db, user.id, "rewards").items()}}


def referral_admin_view(db: Session, ref: Referral) -> dict:
    users = {u.id: u for u in db.execute(select(User).where(User.id.in_([ref.referrer_id, ref.referee_id]))).scalars()}
    a, b = users.get(ref.referrer_id), users.get(ref.referee_id)
    return {"id": ref.id, "status": ref.status, "status_label": REF_STATUS_AR.get(ref.status), "flags": ref.flags,
            "referrer": {"ref": ref.referrer_id, "public_id": a.public_id if a else None, "name": a.display_name if a else None},
            "referee": {"ref": ref.referee_id, "public_id": b.public_id if b else None, "name": b.display_name if b else None,
                        "member": bool(b and b.member_since and not b.member_ended_at)},
            "created_at": iso(ref.created_at), "rewarded_at": iso(ref.rewarded_at)}


def admin_referrals(db: Session, status: str = "") -> dict:
    q = select(Referral).order_by(Referral.created_at.desc())
    if status in REF_STATUS_AR:
        q = q.where(Referral.status == status)
    return {"referrals": [referral_admin_view(db, r) for r in db.execute(q.limit(200)).scalars()],
            "review": db.scalar(select(func.count()).select_from(Referral).where(Referral.status == "review")) or 0,
            "batches": [{"id": b.id, "amount": ledger.from_minor(b.amount_minor), "count": b.count, "note": b.note,
                         "total": ledger.from_minor(b.total_minor or 0), "by": b.created_by, "at": iso(b.created_at),
                         "criteria": json.loads(b.criteria)} for b in db.execute(
                select(RewardBatch).order_by(RewardBatch.created_at.desc()).limit(30)).scalars()]}


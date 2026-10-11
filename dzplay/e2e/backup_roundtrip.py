# Backup round-trip helper (runs inside the app container of the THROWAWAY test stack only).
# `seed` writes known rows, `wipe` deletes users + ideas, `check` prints what is there.
import sys

from sqlalchemy import func, select

from app.config import get_settings
from app.db import Database
from app.models import AdminAuditLog, AdminUser, LedgerEntry, Post, SupportMessage, SupportTicket, User, VerificationRequest
from app.services import admin_auth, audit

settings = get_settings()
db = Database(settings.DATABASE_URL)
db.create_all()
with db.session() as s:
    if sys.argv[1] == "seed":
        u = User(email="roundtrip@example.com", password_hash="x", status="active")
        s.add(u)
        s.flush()
        s.add(Post(author_id=u.id, content="فكرة محفوظة في النسخة الاحتياطية"))
        # V5 tables: support, blue star request, the money ledger
        t = SupportTicket(user_id=u.id, category="other", subject="تذكرة محفوظة")
        s.add(t)
        s.flush()
        s.add(SupportMessage(ticket_id=t.id, author="user", body="نص التذكرة"))
        s.add(VerificationRequest(user_id=u.id, account_type="writer", description="d", reason="r", amount="5.00",
                                  currency="USDT", network="TRC20", wallet="T" + "x" * 33, txid="a" * 64))
        s.add(LedgerEntry(user_id=u.id, kind="earning", amount_minor=1234, currency="USD", created_by="admin:owner"))
        admin_auth.create_admin(s, settings, "owner", "Roundtrip-Admin-Pass-1")
        audit.record(s, "cli", "create_admin", target_type="admin", target_id="owner")
    elif sys.argv[1] == "wipe":
        s.query(Post).delete()
        s.query(User).delete()
    users = s.scalar(select(func.count()).select_from(User))
    posts = [p.content for p in s.execute(select(Post)).scalars()]
    admins = [a.username for a in s.execute(select(AdminUser)).scalars()]
    chain = audit.verify_chain(s)
    v5 = [s.scalar(select(func.count()).select_from(m)) for m in (SupportTicket, SupportMessage, VerificationRequest)]
    money = s.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)))
    print(f"users={users} posts={posts} admins={admins} audit_rows={s.scalar(select(func.count()).select_from(AdminAuditLog))} chain_ok={chain['ok']} v5={v5} ledger={money}")

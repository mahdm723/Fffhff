# V6 phase 1b helper for the THROWAWAY Docker stack (e2e/run_backup_e2e.sh): piped into the app container,
# never run against a real deployment.
#   seed      two users, an old random anonymous chat (kind NULL) between them and a direct chat
#   backdate  pretend the anonymous chats were closed 8 days ago (past LEGACY_ANON_RETENTION_DAYS)
#   check     print what is left
import sys
from datetime import timedelta

from sqlalchemy import func, select, text

from app import clock
from app.config import get_settings
from app.db import Database
from app.models import Conversation, Message, User

db_ = Database(get_settings().DATABASE_URL)
db_.create_all()
now = clock.utcnow()

if sys.argv[1] == "seed":
    with db_.session() as s:
        a = User(email="anon-a@example.com", password_hash="x", status="active")
        b = User(email="anon-b@example.com", password_hash="x", status="active")
        s.add_all([a, b])
        s.flush()
        for kind, body in ((None, "رسالة مجهولة قديمة e2e"), ("direct", "رسالة مباشرة باقية")):
            c = Conversation(initiator_id=a.id, recipient_id=b.id, created_at=now, last_message_at=now, updated_at=now,
                             expires_at=now + timedelta(days=14), last_sender_id=a.id, consecutive_count=1, initiator_sent=True,
                             kind=kind, direct_key=f"{min(a.id, b.id)}:{max(a.id, b.id)}" if kind else None,
                             request_state="accepted" if kind else None)
            s.add(c)
            s.flush()
            s.add(Message(conversation_id=c.id, sender_id=a.id, recipient_id=b.id, content=body, created_at=now,
                          expires_at=now + timedelta(days=7)))
elif sys.argv[1] == "backdate":
    with db_.engine.begin() as conn:
        conn.execute(text("UPDATE app_settings SET value = :v WHERE key = 'schema.v6_anon_closed_at'"),
                     {"v": (now - timedelta(days=8)).isoformat()})

with db_.session() as s:
    anon = s.scalar(select(func.count()).select_from(Conversation).where(Conversation.kind.is_(None)))
    direct = s.scalar(select(func.count()).select_from(Conversation).where(Conversation.kind == "direct"))
    msgs = s.scalar(select(func.count()).select_from(Message))
    print(f"anonymous={anon} direct={direct} messages={msgs}")

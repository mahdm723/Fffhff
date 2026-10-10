# V6 clean-up helper for the THROWAWAY Docker stack (e2e/run_backup_e2e.sh): piped into the app container
# after tests/legacy_v5_schema.py (which defines DDL / LEGACY_TABLES), never run against a real deployment.
# `seed` recreates the old V5 tables with rows (Reel, call, earnings, boost), `check` prints what is left.
import sys
from datetime import datetime, timezone

from sqlalchemy import func, inspect, select, text

from app.config import get_settings
from app.db import Database
from app.models import Post, User

settings = get_settings()
db = Database(settings.DATABASE_URL)
db.create_all()
engine = db.engine
now = datetime.now(timezone.utc).replace(tzinfo=None)

if sys.argv[1] == "seed":
    with db.session() as s:
        u = User(email="v6-legacy@example.com", password_hash="x", status="active", verified_at=now)
        s.add(u)
        s.flush()
        p = Post(author_id=u.id, content="فكرة تبقى بعد التنظيف")
        s.add(p)
        s.commit()
        uid, pid = u.id, p.id
    with engine.begin() as conn:
        for stmt in DDL[engine.dialect.name]:  # noqa: F821 (defined by tests/legacy_v5_schema.py)
            conn.execute(text(stmt))
        conn.execute(text("INSERT INTO reels (id, short_id, kind, caption, status, likes_count, dislikes_count, comments_count, "
                          "views_count, created_at, updated_at, owner_id) VALUES ('r1', 'ab12cd', 'video', 'مقطع قديم e2e', "
                          "'visible', 3, 0, 0, 9, :t, :t, :u)"), {"t": now, "u": uid})
        conn.execute(text("INSERT INTO calls (id, caller_id, callee_id, kind, video_used, state, created_at) "
                          "VALUES ('c1', :u, :u, 'audio', false, 'ended', :t)"), {"t": now, "u": uid})
        conn.execute(text("INSERT INTO ledger_entries (user_id, kind, amount_minor, currency, created_by, created_at) "
                          "VALUES (:u, 'payout', -500, 'USDT', 'admin:x', :t)"), {"t": now, "u": uid})
        conn.execute(text("UPDATE posts SET boost_likes = 77 WHERE id = :p"), {"p": pid})

with engine.connect() as conn:
    tables = set(inspect(engine).get_table_names())
    old = sorted(tables & {"reels", "reel_assets", "reel_reactions", "reel_comments", "reel_views", "calls",
                           "monetization_applications"})
    with db.session() as s:
        star = s.scalar(select(func.count()).select_from(User).where(User.email == "v6-legacy@example.com",
                                                                     User.verified_at.is_not(None)))
        idea = s.scalar(select(func.count()).select_from(Post).where(Post.content == "فكرة تبقى بعد التنظيف"))
    payouts = conn.execute(text("SELECT COUNT(*) FROM ledger_entries WHERE kind = 'payout'")).scalar()
    boost = conn.execute(text("SELECT COUNT(*) FROM posts WHERE boost_likes IS NOT NULL")).scalar()
    print(f"old_tables={old} payouts={payouts} boosted={boost} star_kept={star} idea={idea}")

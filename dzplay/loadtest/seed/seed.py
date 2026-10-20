"""V6 phase 21: fill a STAGING database with a realistic community — fast (PostgreSQL COPY), repeatable (fixed seed).

    APP_ENV=staging python loadtest/seed/seed.py --database-url postgresql://… [--scale 1.0] [--reset] [--out DIR]

Run inside the staging app container (loadtest/run.sh does it): it imports the app for the same rules (names, search
keys, public ids, the password hash, the payment settings). At --scale 1:
  50,000 users (Arabic / Latin names, 70 % with a picture), 150,000 ideas (10 % with a picture), 600,000 comments and
  replies, 2,000,000 likes / dislikes (the post counters match), 30,000 direct chats with ~10 messages each,
  ~300,000 notifications, 2,000 members (requests + ledger), 1,500 reward balances, 300 pending withdrawals, an open
  red envelope round with 5,000 entries, a pinned post of the official account.
Every seeded user has the password LT_PASSWORD; 20,000 of them get a session for the load generator
(out/vu_users.csv: token, email, public id, profile ref, language). Pictures point at the mock Telegram.
Refuses anything but a database named *_staging (loadtest/lib/safety.py).
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))  # dzplay/ (the app) and loadtest/
from loadtest.lib import safety  # noqa: E402

LT_PASSWORD = "LoadTest-Pass-2026!"
NOW = datetime.now(timezone.utc).replace(tzinfo=None)

FIRST_AR = ["أحمد", "محمد", "يوسف", "عمر", "علي", "خالد", "سارة", "فاطمة", "مريم", "ليلى", "نور", "هدى", "ياسين", "رياض",
            "إيمان", "أمين", "سمير", "كريم", "منى", "رانيا", "بلال", "زكريا", "هشام", "وليد", "أسماء", "خديجة", "إلياس",
            "عبد الله", "سفيان", "نسرين"]
LAST_AR = ["بن علي", "العربي", "بوزيد", "الجزائري", "حداد", "منصور", "سعيدي", "قاسمي", "بلقاسم", "زروقي", "مرابط",
           "شريف", "دحماني", "بوعلام", "حمدي", "عيسى", "طالب", "يحيى", "رحماني", "بن يوسف"]
FIRST_LAT = ["Yacine", "Amine", "Sofiane", "Karim", "Nadia", "Sarah", "Lina", "Rayan", "Ines", "Walid", "Mehdi", "Yanis",
             "Adam", "Nour", "Samy", "Lea", "Omar", "Ilyes", "Anis", "Meriem"]
LAST_LAT = ["Trader", "Benali", "Haddad", "Crypto", "Mansouri", "Saidi", "Kaci", "Bouzid", "Cherif", "Hamdi", "Rahmani", "Larbi"]
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "TON", "AVAX", "LINK", "ARB", "SUI", "PEPE", "INJ", "TIA", "NEAR"]
IDEAS = {
    "ar": ["تحليل {c}: الدعم عند {a} والمقاومة عند {b}. أنتظر كسر المقاومة قبل الدخول، وإدارة رأس المال أولًا.",
           "رأيي في {c} هذا الأسبوع: السوق متذبذب، والأفضل الانتظار حتى يتضح الاتجاه. ما رأيكم؟",
           "تعلمت اليوم درسًا مهمًا: لا تدخل صفقة بدون وقف خسارة. خسرت جزءًا صغيرًا على {c} وتعلمت الكثير.",
           "أخبار: حجم تداول {c} ارتفع بشكل ملحوظ خلال 24 ساعة. هل هي بداية موجة جديدة؟",
           "سؤال للمتداولين: كيف توزعون محفظتكم بين {c} و{d}؟ أنا أحاول التنويع وتقليل المخاطرة.",
           "ملاحظة على الإطار الأسبوعي لـ{c}: نموذج مثلث صاعد، والهدف التقريبي {b} إن تم الاختراق."],
    "fr": ["Analyse {c} : support à {a}, résistance à {b}. J'attends la cassure avant d'entrer.",
           "Mon avis sur {c} cette semaine : marché indécis, patience. Et vous ?",
           "Leçon du jour : toujours un stop-loss. Petite perte sur {c}, gros apprentissage."],
    "en": ["{c} analysis: support at {a}, resistance at {b}. Waiting for a breakout before entering.",
           "My take on {c} this week: choppy market, better to wait for a clear trend. Thoughts?",
           "News: {c} 24h volume is up sharply. Start of a new leg?"],
}
COMMENTS = ["تحليل ممتاز، شكرًا", "أتفق معك", "رأي مختلف: أرى هبوطًا قبل الصعود", "متى تتوقع الاختراق؟", "مفيد جدًا 👍",
            "إدارة رأس المال أهم شيء", "شكرًا على المشاركة", "هل لديك مصدر لهذا الخبر؟", "Good point", "Merci pour l'analyse",
            "أنا دخلت من {a}", "الهدف القادم {b} بإذن الله", "انتبه للأخبار هذا الأسبوع", "متابع 👀"]
MESSAGES = ["السلام عليكم", "مرحبًا، رأيت تحليلك عن {c}", "هل تتداول على المدى القصير؟", "شكرًا على النصيحة",
            "ما رأيك في {c} الآن؟", "أوافقك", "سأتابع الشارت وأخبرك", "Salut, ça va ?", "Thanks!", "تمام 👍"]


class Rows:
    """Rows of one table, written with COPY (tab-separated text)."""

    def __init__(self, table: str, columns: list[str]):
        self.table, self.columns, self.buf, self.count = table, columns, io.StringIO(), 0

    def add(self, *values) -> None:
        out = []
        for v in values:
            if v is None:
                out.append("\\N")
            elif isinstance(v, bool):
                out.append("t" if v else "f")
            elif isinstance(v, datetime):
                out.append(v.isoformat(sep=" "))
            else:
                out.append(str(v).replace("\\", "\\\\").replace("\t", " ").replace("\n", "\\n").replace("\r", ""))
        self.buf.write("\t".join(out) + "\n")
        self.count += 1

    def copy(self, cur) -> int:
        self.buf.seek(0)
        cols = ", ".join(self.columns)
        with cur.copy(f"COPY {self.table} ({cols}) FROM STDIN") as cp:
            while chunk := self.buf.read(1 << 20):
                cp.write(chunk)
        n, self.buf, self.count = self.count, io.StringIO(), 0
        return n


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    p.add_argument("--scale", type=float, default=float(os.environ.get("LT_SCALE", "1")))
    p.add_argument("--reset", action="store_true", help="empty the staging tables first")
    p.add_argument("--out", default=str(HERE.parent / "out"))
    p.add_argument("--seed", type=int, default=21)
    a = p.parse_args()
    safety.require_staging_env()
    dsn = a.database_url.replace("postgresql+psycopg://", "postgresql://")
    if not dsn.startswith("postgresql://"):
        raise safety.Unsafe("seeding needs PostgreSQL (DATABASE_URL)")

    import psycopg

    from app.config import PRIVACY_VERSION
    from app.models import PUBLIC_ID_ALPHABET
    from app.security.passwords import hash_password
    from app.services import chain, names, notify

    rnd = random.Random(a.seed)
    keep = timedelta(days=notify.KEEP_DAYS)  # older ones: the app's cleanup has already deleted them
    S = a.scale
    n_users, n_posts, n_comments = int(50_000 * S), int(150_000 * S), int(600_000 * S)
    n_reactions, n_convs, n_members = int(2_000_000 * S), int(30_000 * S), int(2_000 * S)
    n_rewards, n_withdraw, n_giveaway, n_vu = int(1_500 * S), int(300 * S), int(5_000 * S), int(20_000 * S)

    def rid() -> str:  # like new_public_id(): 22 url-safe characters
        return base64.urlsafe_b64encode(rnd.getrandbits(128).to_bytes(16, "big")).decode().rstrip("=")

    def media_id() -> str:  # like MediaItem's own ids (32 hex characters: media.ASSET_ID)
        return f"{rnd.getrandbits(128):032x}"

    def ago(days: float) -> datetime:
        return NOW - timedelta(seconds=days * 86400)

    def recent(max_days: float) -> datetime:  # more activity lately
        return ago(max_days * (rnd.random() ** 2))

    t0 = time.time()
    with psycopg.connect(dsn, autocommit=False) as conn, conn.cursor() as cur:
        safety.check_database(cur)
        cur.execute("SELECT id FROM users WHERE is_official IS TRUE")
        official = cur.fetchone()
        if official is None:
            raise SystemExit("start the staging app once first (it creates the tables and the official account)")
        official_id = official[0]
        if a.reset:
            cur.execute("SELECT count(*) FROM users")
            print(f"reset: emptying the staging tables ({cur.fetchone()[0]} users)", flush=True)
            cur.execute("""TRUNCATE post_reactions, comments, notifications, messages, conversations, sessions, profile_refs,
                           ledger_entries, membership_requests, withdrawal_requests, giveaway_entries, giveaway_rounds,
                           media_items, user_devices, referrals, signup_pending RESTART IDENTITY CASCADE""")
            cur.execute("DELETE FROM posts")
            cur.execute("DELETE FROM users WHERE is_official IS NOT TRUE AND is_system IS NOT TRUE")
        cur.execute("SELECT count(*) FROM users WHERE is_official IS NOT TRUE AND is_system IS NOT TRUE")
        if cur.fetchone()[0]:
            raise SystemExit("already seeded (use --reset to start again)")

        pw_hash = hash_password(LT_PASSWORD)  # one hash for everyone (50k argon2 hashes would take ~40 min)
        total = {}

        # ---------------------------------------------------------------- users (+ pictures, refs)
        users = Rows("users", ["id", "email", "email_canonical", "password_hash", "status", "created_at", "last_active_at",
                               "messages_sent", "messages_received", "conversations_count", "privacy_ack_version",
                               "display_name", "name_norm", "public_id", "gender", "age_confirmed_at", "searchable_by_name",
                               "accept_direct", "avatar_media_id", "member_since", "verified_at", "verified_by", "language",
                               "email_verified_at", "signup_verified_via"])
        media = Rows("media_items", ["id", "owner_id", "owner_public_id", "purpose", "kind", "state", "ready", "attached_type",
                                     "attached_id", "attached_at", "size", "width", "height", "tg_file_id", "tg_unique_id",
                                     "created_at", "ready_at", "hidden", "reports_count"])
        refs = Rows("profile_refs", ["user_id", "ref", "created_at"])
        uid, upub, uref, ulang, uemail = [], [], [], [], []
        member_idx = set(rnd.sample(range(n_users), n_members))
        used_pub = set()
        for i in range(n_users):
            u = rid()
            lang = rnd.choices(["ar", "fr", "en"], [80, 12, 8])[0]
            if lang == "ar" or rnd.random() < 0.2:
                name = f"{rnd.choice(FIRST_AR)} {rnd.choice(LAST_AR)}"
            else:
                name = f"{rnd.choice(FIRST_LAT)} {rnd.choice(LAST_LAT)}"
            name = name[:20]
            while True:
                pub = "DZ-" + "".join(rnd.choice(PUBLIC_ID_ALPHABET) for _ in range(6))
                if pub not in used_pub:
                    used_pub.add(pub)
                    break
            email = f"lt{i}@staging.test"
            created = recent(365)
            avatar = None
            if rnd.random() < 0.7:
                avatar = media_id()
                media.add(avatar, u, pub, "avatar", "image", "attached", False, "user", u, created, 9000, 256, 256,
                          f"lt-avatar-{i}", f"lta{i}", created, created, False, 0)
            member = i in member_idx
            users.add(u, email, email, pw_hash, "active", created, recent(30), 0, 0, 0, PRIVACY_VERSION, name,
                      names.search_key(name), pub, rnd.choice(["male", "female", "unspecified"]), created, True,
                      "everyone", avatar, recent(200) if member else None, recent(200) if member else None,
                      "membership" if member else None, lang, created, "email")
            ref = rid()
            refs.add(u, ref, created)
            uid.append(u), upub.append(pub), uref.append(ref), ulang.append(lang), uemail.append(email)
        total["users"] = users.copy(cur)
        total["profile_refs"] = refs.copy(cur)
        print(f"users: {total['users']} ({time.time() - t0:.0f}s)", flush=True)

        # active authors: a minority writes most ideas (Pareto-like)
        def author() -> int:
            return min(n_users - 1, int(rnd.paretovariate(1.2)) - 1 + rnd.randrange(n_users) // 20)

        # ---------------------------------------------------------------- posts (+ pictures)
        posts = Rows("posts", ["id", "author_id", "content", "status", "created_at", "updated_at", "likes_count",
                               "dislikes_count", "comments_count", "unseen_comments_count", "media_id", "pinned_until"])
        pid, pauthor, pcreated = [], [], []
        for i in range(n_posts):
            pid.append(rid())
            pauthor.append(rnd.randrange(n_users) if rnd.random() < 0.5 else author())
            pcreated.append(recent(180))
        # reactions per post: heavy-tailed (a few ideas get most of them)
        weights = [rnd.paretovariate(1.5) * (1.5 if (NOW - pcreated[k]).days < 7 else 1) for k in range(n_posts)]
        wsum = sum(weights)
        def share(x: float) -> int:  # random rounding: the totals reach the targets
            return int(x) + (rnd.random() < x - int(x))

        r_count = [min(n_users - 1, share(n_reactions * w / wsum)) for w in weights]
        c_weights = [w ** 0.8 for w in weights]
        cwsum = sum(c_weights)
        c_count = [share(n_comments * w / cwsum) for w in c_weights]

        for k in range(n_posts):  # the posts first (reactions and comments point at them), counters below
            text = rnd.choice(IDEAS[ulang[pauthor[k]]]).format(
                c=rnd.choice(COINS), d=rnd.choice(COINS), a=rnd.randint(1, 900), b=rnd.randint(900, 70000))
            mid = None
            if rnd.random() < 0.1:
                mid = media_id()
                media.add(mid, uid[pauthor[k]], upub[pauthor[k]], "idea", "image", "attached", False, "post", pid[k],
                          pcreated[k], 60000, 1080, 864, f"lt-post-{k}", f"ltp{k}", pcreated[k], pcreated[k], False, 0)
            posts.add(pid[k], uid[pauthor[k]], text, "visible", pcreated[k], pcreated[k], 0, 0, 0, 0, mid, None)
        posts.add(rid(), official_id, "إعلان رسمي: هذه نسخة الاختبار (staging) من المنصة.", "visible", NOW, NOW, 0, 0, 0, 0,
                  None, NOW + timedelta(days=30))
        total["posts"] = posts.copy(cur)
        total["media_items"] = media.copy(cur)

        reactions = Rows("post_reactions", ["post_id", "user_id", "reaction_type", "created_at", "updated_at"])  # id: serial
        likes, dislikes = [0] * n_posts, [0] * n_posts
        for k in range(n_posts):
            n = r_count[k]
            if not n:
                continue
            for u in rnd.sample(range(n_users), n):
                kind = "like" if rnd.random() < 0.85 else "dislike"
                if kind == "like":
                    likes[k] += 1
                else:
                    dislikes[k] += 1
                when = pcreated[k] + (NOW - pcreated[k]) * rnd.random() ** 3
                reactions.add(pid[k], uid[u], kind, when, when)
            if reactions.count >= 200_000:
                total["post_reactions"] = total.get("post_reactions", 0) + reactions.copy(cur)
        total["post_reactions"] = total.get("post_reactions", 0) + reactions.copy(cur)

        comments = Rows("comments", ["id", "post_id", "author_id", "content", "created_at", "visibility", "parent_id",
                                     "reply_to_user_id"])
        notifs = Rows("notifications", ["id", "user_id", "kind", "created_at", "actor_id", "post_id", "comment_id", "read_at"])
        ccount = [0] * n_posts
        for k in range(n_posts):
            tops: list[tuple[str, int]] = []
            for _ in range(c_count[k]):
                u = rnd.randrange(n_users)
                cid = rid()
                when = pcreated[k] + (NOW - pcreated[k]) * rnd.random() ** 2
                text = rnd.choice(COMMENTS).format(a=rnd.randint(10, 900), b=rnd.randint(900, 2000))
                if tops and rnd.random() < 0.3:
                    parent, parent_author = rnd.choice(tops)
                    comments.add(cid, pid[k], uid[u], text, when, "public", parent, uid[parent_author])
                    if parent_author != u and rnd.random() < 0.5 and NOW - when < keep:  # never one's own action
                        notifs.add(rid(), uid[parent_author], "reply", when, uid[u], pid[k], cid,
                                   when if rnd.random() < 0.8 else None)
                else:
                    comments.add(cid, pid[k], uid[u], text, when, "public", None, None)
                    tops.append((cid, u))
                    if pauthor[k] != u and rnd.random() < 0.5 and NOW - when < keep:
                        notifs.add(rid(), uid[pauthor[k]], "comment", when, uid[u], pid[k], cid,
                                   when if rnd.random() < 0.8 else None)
                ccount[k] += 1
            if comments.count >= 200_000:
                total["comments"] = total.get("comments", 0) + comments.copy(cur)
                total["notifications"] = total.get("notifications", 0) + notifs.copy(cur)
        total["comments"] = total.get("comments", 0) + comments.copy(cur)

        cur.execute("CREATE TEMP TABLE lt_post_counts (id varchar(32), l int, d int, c int, u int) ON COMMIT DROP")
        pc = Rows("lt_post_counts", ["id", "l", "d", "c", "u"])
        for k in range(n_posts):
            if likes[k] or dislikes[k] or ccount[k]:
                pc.add(pid[k], likes[k], dislikes[k], ccount[k], min(ccount[k], rnd.randint(0, 3)))
        pc.copy(cur)
        cur.execute("UPDATE posts p SET likes_count = c.l, dislikes_count = c.d, comments_count = c.c, "
                    "unseen_comments_count = c.u FROM lt_post_counts c WHERE p.id = c.id")
        print(f"posts/reactions/comments ({time.time() - t0:.0f}s)", flush=True)

        # ---------------------------------------------------------------- direct chats
        convs = Rows("conversations", ["id", "initiator_id", "recipient_id", "status", "created_at", "last_message_at",
                                       "updated_at", "expires_at", "initiator_hidden", "recipient_hidden", "consecutive_count",
                                       "kind", "direct_key", "request_state", "last_sender_id", "initiator_last_read_at",
                                       "recipient_last_read_at"])
        msgs = Rows("messages", ["id", "conversation_id", "sender_id", "recipient_id", "content", "created_at", "expires_at",
                                 "delivered_at", "read_at"])
        sent, recv, nconv = [0] * n_users, [0] * n_users, [0] * n_users
        pairs = set()
        while len(pairs) < n_convs:
            x, y = rnd.randrange(n_users), author() if rnd.random() < 0.4 else rnd.randrange(n_users)
            if x != y:
                pairs.add((x, y))
        for x, y in pairs:
            cid = rid()
            n = max(1, int(rnd.expovariate(1 / 10)))
            start = ago(rnd.uniform(0.2, 6.5))  # messages live 7 days
            accepted = rnd.random() < 0.9
            t = start
            last_sender = x
            for j in range(n if accepted else 1):
                s, r = (x, y) if j == 0 or rnd.random() < 0.55 else (y, x)
                t = t + timedelta(seconds=rnd.randint(20, 3600))
                if t > NOW:
                    t = NOW - timedelta(seconds=rnd.randint(1, 600))
                read = t if rnd.random() < 0.8 else None
                msgs.add(rid(), cid, uid[s], uid[r], rnd.choice(MESSAGES).format(c=rnd.choice(COINS)), t,
                         t + timedelta(days=7), t, read)
                sent[s] += 1
                recv[r] += 1
                last_sender = s
            convs.add(cid, uid[x], uid[y], "active", start, t, t, t + timedelta(days=14), False, False, 0, "direct",
                      ":".join(sorted((uid[x], uid[y]))), "accepted" if accepted else "pending", uid[last_sender], t, t)
            nconv[x] += 1
            nconv[y] += 1
        total["conversations"] = convs.copy(cur)  # before the messages (foreign key)
        total["messages"] = msgs.copy(cur)
        total["notifications"] = total.get("notifications", 0) + notifs.copy(cur)
        print(f"chats/notifications ({time.time() - t0:.0f}s)", flush=True)

        # ---------------------------------------------------------------- money (members, rewards, withdrawals)
        tron_wallet = chain.tron_from_hex20(os.environ.get("MOCK_TRON_WALLET_HEX", "11" * 20))
        for key, value in (("pay.currency", "USDT"), ("pay.network", "TRC20"), ("pay.wallet", tron_wallet),
                           ("pay.explorer", "https://tronscan.org/#/transaction/{txid}"), ("pay.note", "staging")):
            cur.execute("INSERT INTO app_settings (key, value, updated_at, updated_by) VALUES (%s, %s, now(), 'loadtest') "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
        reqs = Rows("membership_requests", ["id", "user_id", "user_public_id", "amount_minor", "currency", "network", "wallet",
                                            "txid", "status", "created_at", "decided_at", "decided_by"])
        ledger = Rows("ledger_entries", ["user_id", "kind", "amount_minor", "currency", "created_by", "created_at", "note",
                                         "account", "source_type", "source_id", "available_at"])
        for i in sorted(member_idx):
            req, when = rid(), recent(200)
            reqs.add(req, uid[i], upub[i], 5000, "USDT", "TRC20", tron_wallet, hashlib.sha256(req.encode()).hexdigest(),
                     "accepted", when, when, "admin:loadtest")
            ledger.add(uid[i], "mem_pay", 5000, "USDT", "admin:loadtest", when, "TRC20", "membership", "mem_request", req, None)
        rich = rnd.sample(range(n_users), n_rewards)
        for i in rich:
            when = recent(90)
            ledger.add(uid[i], "contest", rnd.choice([1000, 2000, 2500, 5000, 10000]), "USDT", "admin:loadtest", when,
                       "seed", "rewards", "seed", rid(), None)
        wds = Rows("withdrawal_requests", ["id", "user_id", "user_public_id", "network", "address", "amount_minor", "fee_minor",
                                           "status", "created_at"])
        for i in rich[:n_withdraw]:
            w, when = rid(), recent(10)
            wds.add(w, uid[i], upub[i], "TRC20", tron_wallet, 1000, 100, "pending", when)
            ledger.add(uid[i], "withdraw", -1000, "USDT", f"user:{upub[i]}", when, "TRC20", "rewards", "withdraw", w, None)
        total["membership_requests"] = reqs.copy(cur)
        total["withdrawal_requests"] = wds.copy(cur)
        total["ledger_entries"] = ledger.copy(cur)

        # ---------------------------------------------------------------- red envelope
        rnd_id = rid()
        cur.execute("INSERT INTO giveaway_rounds (id, title, winners_count, ends_at, status, created_by, created_at) "
                    "VALUES (%s, %s, %s, %s, 'open', 'admin:loadtest', %s)",
                    (rnd_id, "الظرف الأحمر (staging)", 10, NOW + timedelta(days=7), NOW - timedelta(days=1)))
        entries = Rows("giveaway_entries", ["id", "round_id", "user_id", "email", "created_at", "email_verified_at"])
        for i in rnd.sample(range(n_users), n_giveaway):
            when = recent(1)
            entries.add(rid(), rnd_id, uid[i], uemail[i], when, when)
        total["giveaway_entries"] = entries.copy(cur)

        # ---------------------------------------------------------------- counters, sessions for the generator
        cur.execute("CREATE TEMP TABLE lt_counts (id varchar(32), s int, r int, c int) ON COMMIT DROP")
        counts = Rows("lt_counts", ["id", "s", "r", "c"])
        for i in range(n_users):
            if sent[i] or recv[i] or nconv[i]:
                counts.add(uid[i], sent[i], recv[i], nconv[i])
        counts.copy(cur)
        cur.execute("UPDATE users u SET messages_sent = c.s, messages_received = c.r, conversations_count = c.c "
                    "FROM lt_counts c WHERE u.id = c.id")
        sessions = Rows("sessions", ["token_hash", "user_id", "created_at", "last_seen_at", "expires_at"])
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        vu = sorted(rnd.sample(range(n_users), n_vu))
        with open(out / "vu_users.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["token", "email", "public_id", "ref", "lang"])
            for i in vu:
                token = base64.urlsafe_b64encode(rnd.getrandbits(256).to_bytes(32, "big")).decode().rstrip("=")
                sessions.add(hashlib.sha256(token.encode()).hexdigest(), uid[i], NOW, NOW, NOW + timedelta(days=30))
                w.writerow([token, uemail[i], upub[i], uref[i], ulang[i]])
        total["sessions"] = sessions.copy(cur)
        (out / "meta.json").write_text(json.dumps({"password": LT_PASSWORD, "scale": S, "counts": total,
                                                   "seeded_at": NOW.isoformat()}, ensure_ascii=False, indent=1))
        conn.commit()
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("ANALYZE")
    print(json.dumps(total, indent=1))
    print(f"seeded in {time.time() - t0:.0f}s → {Path(a.out) / 'vu_users.csv'}")


if __name__ == "__main__":
    main()

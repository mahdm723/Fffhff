"""Central configuration.

Every tunable value (limits, TTLs, security knobs) lives here and
is read from environment variables or a `.env` file — nothing is hard-coded in
the business logic. See `.env.example` for documentation of each value.
"""

from __future__ import annotations

import secrets
from functools import lru_cache

from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

HOUR = 3600
DAY = 24 * HOUR



# First path segments the app itself uses; ADMIN_PATH may not shadow them.
RESERVED_PATHS = {"api", "js", "css", "fonts", "icons", "download", "media", "admin", "healthz", ".well-known", "policies", "vendor", "r",
                  "sw.js", "manifest.webmanifest", "index.html"}


# Bump when the privacy policy changes in a way users must be told about; users
# who acknowledged an older version see the new notice once (see /api/me).
PRIVACY_VERSION = 5  # V5: pictures, Telegram storage, chat pictures, star, payments, earnings, account deletion


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- application -------------------------------------------------------
    APP_NAME: str = "DALTA.BIT"
    ENV: str = "development"  # "development" | "production" | "test"
    SECRET_KEY: str = ""
    DATABASE_URL: str = "sqlite:///./dzplay.db"
    REDIS_URL: str = ""  # optional: enables shared rate limits + realtime across workers
    LOG_LEVEL: str = "INFO"

    # --- HTTP / cookies / proxies -----------------------------------------
    COOKIE_SECURE: bool | None = None  # default: True in production
    SESSION_COOKIE_NAME: str = "dz_session"
    SESSION_DURATION: int = 30 * DAY
    TRUST_PROXY_HEADERS: bool = False  # read client IP from X-Forwarded-For
    TRUSTED_PROXY_COUNT: int = 1  # number of reverse proxies in front of the app
    ALLOWED_ORIGINS: str = ""  # extra origins allowed for WebSocket (comma separated)

    # --- registration / login protection ----------------------------------
    PASSWORD_MIN_LENGTH: int = 8
    PASSWORD_MAX_LENGTH: int = 128
    LOGIN_MAX_ATTEMPTS: int = 3  # consecutive failures before a block
    IP_BLOCK_DURATION: int = 4 * HOUR
    # "ip_account": 3 failures block that IP for that account only (protects
    #   innocent users behind NAT/VPN); the whole IP is blocked only after
    #   IP_MAX_FAILED_LOGINS failures across accounts.
    # "ip": literal mode — LOGIN_MAX_ATTEMPTS failures block the whole IP.
    LOGIN_BLOCK_SCOPE: str = "ip_account"
    IP_MAX_FAILED_LOGINS: int = 10
    LOGIN_FAILURE_WINDOW: int = 4 * HOUR
    ACCOUNT_MAX_FAILED_LOGINS: int = 10  # per account, from any IP
    ACCOUNT_LOCK_DURATION: int = 15 * 60
    LOGIN_BACKOFF_BASE: float = 1.0  # progressive delay: base * 2^(failures-1)
    LOGIN_BACKOFF_MAX: float = 30.0
    MAX_ACCOUNTS_PER_IP: int = 2
    ACCOUNTS_PER_IP_WINDOW: int = 4 * HOUR
    TRUSTED_IPS: str = ""  # comma separated IPs exempt from IP limits (e.g. office)

    # --- anti-bot (self-hosted proof-of-work challenge) --------------------
    ANTIBOT_ENABLED: bool = True
    POW_MAX_NUMBER: int = 60_000  # normal difficulty (~0.1–0.5 s on a phone)
    POW_ELEVATED_MAX_NUMBER: int = 400_000  # used for IPs with recent failures
    POW_CHALLENGE_TTL: int = 300

    # --- Google Sign-In (optional) ----------------------------------------
    GOOGLE_CLIENT_ID: str = ""

    # --- messages / anti-spam ---------------------------------------------
    MAX_MESSAGE_LENGTH: int = 1000
    LINK_POLICY: str = "reject"  # "reject" | "allow_plain" (never clickable)
    MAX_MESSAGES_PER_MINUTE: int = 10
    MAX_MESSAGES_PER_HOUR: int = 120
    MAX_CONSECUTIVE_MESSAGES: int = 8  # in a conversation, before the other side replies
    DUPLICATE_MESSAGE_WINDOW: int = 10 * 60  # identical ideas posted again within this window are rejected
    MAX_REPORTS_PER_HOUR: int = 10
    REPORT_AUTO_SUSPEND_THRESHOLD: int = 5  # distinct reporters in 24h; 0 disables

    # --- public ideas (posts) ---------------------------------------------------
    MAX_POST_LENGTH: int = 2000
    MAX_COMMENT_LENGTH: int = 500
    MAX_POSTS_PER_HOUR: int = 5
    MAX_POSTS_PER_DAY: int = 20
    MAX_COMMENTS_PER_MINUTE: int = 6
    MAX_COMMENTS_PER_HOUR: int = 60
    MAX_REACTIONS_PER_MINUTE: int = 60
    # feed ranking: weighted random draw (randomness first, then freshness/engagement)
    FEED_PAGE_SIZE: int = 15
    FEED_CANDIDATE_POOL: int = 500  # most recent posts considered for one feed session
    FEED_FRESHNESS_HALF_LIFE_HOURS: float = 24.0
    FEED_FRESHNESS_FLOOR: float = 0.15  # older posts keep at least this much weight
    FEED_ENGAGEMENT_WEIGHT: float = 0.35  # how much likes help (log scale)
    FEED_ENGAGEMENT_CAP: float = 2.5  # max boost, so popular posts don't always win
    FEED_SEEN_PENALTY: float = 0.5  # posts you already reacted to
    FEED_OWN_POST_PENALTY: float = 0.5

    # --- temporary storage (TTL) -------------------------------------------
    MESSAGE_TTL: int = 7 * DAY  # max time a message stays on the server
    MESSAGE_TTL_AFTER_READ: int = 1 * DAY  # shortened once the recipient reads it
    CONVERSATION_IDLE_TTL: int = 14 * DAY  # conversation removed after this much inactivity
    # V6 phase 1b: old anonymous chats stay read-only this long, then deploy/v6-cleanup.sh anon exports + deletes them
    LEGACY_ANON_RETENTION_DAYS: int = 7
    CLEANUP_INTERVAL: int = 300
    SECURITY_EVENT_RETENTION: int = 30 * DAY
    REPORT_RETENTION: int = 90 * DAY

    # --- web push (optional) -----------------------------------------------
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_SUBJECT: str = "mailto:admin@example.com"
    # Browser push services the server may POST to (anything else would let users aim requests at internal hosts).
    PUSH_ALLOWED_HOSTS: str = ("fcm.googleapis.com,push.services.mozilla.com,notify.windows.com,push.apple.com,"
                               "updates.push.services.mozilla.com")

    # --- Android app (Trusted Web Activity) -------------------------------------
    # Digital Asset Links: lets the DZPLAY Android app open this site full screen.
    ANDROID_APP_PACKAGE: str = "io.dzplay.app"
    ANDROID_CERT_SHA256: str = (  # public fingerprint(s) of the APK signing key, comma separated
        "46:15:BE:65:23:30:C9:0A:C6:2C:C3:2C:E5:6B:0C:C0:CB:03:2B:8B:37:E7:13:86:0C:66:D0:A0:FA:EC:79:BF"
    )

    # --- admin panel (separate admin accounts + TOTP 2FA) ---------------------
    PUBLIC_URL: str = ""  # https://your-domain (set by the installer); used to register the Telegram webhook
    ADMIN_PATH: str = ""  # secret URL prefix of the panel, e.g. /panel-x7f3k9q2; empty disables the panel
    ADMIN_IP_ALLOWLIST: str = ""  # optional comma-separated IPs/CIDRs allowed to reach the panel
    ADMIN_SESSION_TTL: int = 12 * HOUR  # absolute lifetime of an admin session
    ADMIN_SESSION_IDLE: int = 30 * 60  # admin session ends after this much inactivity
    ADMIN_LOGIN_MAX_FAILURES: int = 5  # wrong logins per network (and per username) ...
    ADMIN_LOGIN_WINDOW: int = 15 * 60  # ... within this window lock the panel login for that window
    AUDIT_LOG_RETENTION: int = 365 * DAY
    ADMIN_API_PER_MINUTE: int = 240  # requests per admin session per minute (the panel needs ~10 per screen)
    ADMIN_API_ANON_PER_MINUTE: int = 30  # unauthenticated panel API requests per network per minute

    # --- Telegram bot (moderation, password-recovery requests) -----------------
    TELEGRAM_BOT_TOKEN: str = ""  # secret: .env only
    TELEGRAM_ADMIN_CHAT_ID: str = ""  # only this chat may control the bot
    TELEGRAM_WEBHOOK_SECRET: str = ""  # secret: sent by Telegram in X-Telegram-Bot-Api-Secret-Token
    TELEGRAM_API_BASE: str = "https://api.telegram.org"  # or a Local Bot API server
    TELEGRAM_MAX_FILE_MB: int = 20  # getFile limit of the public Bot API

    # --- media cache + processing ----------------------------------------------
    MEDIA_CACHE_DIR: str = "./media-cache"
    MEDIA_CACHE_MAX_GB: float = 5.0
    MEDIA_CACHE_TTL: int = 14 * DAY  # files unused for this long are deleted (refetched on demand)
    MEDIA_URL_TTL: int = 2 * HOUR  # lifetime of signed media URLs
    IMAGE_MAX_SIDE: int = 1440
    IMAGE_QUALITY: int = 82
    IMAGE_MAX_PIXELS: int = 40_000_000  # decompression-bomb guard

    # --- password recovery (admin-assisted via Telegram, code by e-mail) -------
    RESET_CODE_TTL: int = 24 * HOUR
    RESET_MAX_REQUESTS: int = 3  # requests per network within RESET_IP_WINDOW before a block
    RESET_IP_WINDOW: int = 4 * HOUR
    RESET_IP_BLOCK_DURATION: int = 4 * HOUR
    RESET_MAX_PER_EMAIL: int = 3  # requests per e-mail within RESET_EMAIL_WINDOW
    RESET_EMAIL_WINDOW: int = DAY
    RESET_MAX_CODE_ATTEMPTS: int = 5  # wrong codes before the code is cancelled
    RESET_VERIFY_PER_IP_PER_HOUR: int = 20
    RESET_TOKEN_TTL: int = 15 * 60  # after a correct code, time to choose the new password
    RESET_CODE_LENGTH: int = 6

    # --- e-mail (SMTP) -------------------------------------------------------------
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""  # secret: .env only
    SMTP_FROM: str = ""  # e.g. "DALTA.BIT <no-reply@example.com>"
    SMTP_SECURITY: str = "starttls"  # starttls | ssl | none (none only for local testing)
    SMTP_TIMEOUT: int = 20

    # --- engagement control (admin panel) -------------------------------------------
    SYSTEM_ACCOUNTS: int = 25  # internal accounts team comments are posted from (shown as "dzplay")
    ENGAGEMENT_TICK_SECONDS: int = 60  # how often spread-out team comment batches advance
    ENGAGEMENT_MAX_DURATION_HOURS: int = 30 * 24
    ENGAGEMENT_MAX_AMOUNT: int = 1_000_000  # per target and metric
    ENGAGEMENT_MAX_TARGETS: int = 200  # per bulk operation
    ENGAGEMENT_MAX_COMMENTS: int = 200  # per target per operation

    # --- profile ---------------------------------------------------------------------
    FOOTER_TEXT: str = ""  # optional line at the bottom of «حسابي» (empty = none)

    # --- V4: names, public ID, search, direct messages ---------------------------
    DEFAULT_DISPLAY_NAME: str = "dzplay"
    NAME_MIN_LENGTH: int = 3
    NAME_MAX_LENGTH: int = 20
    NAME_CHANGE_COOLDOWN_DAYS: int = 14  # going back to the default name is always allowed
    # Compared after normalization (case, lookalike letters, spaces/underscores removed)
    NAME_RESERVED: str = ("dzplay,dzplay الرسمي,dz play,دلتا بت,دلتابت,دلتا بيت,دلتابيت,dalta bit,official,admin,administrator,support,moderator,mod,staff,team,"
                          "system,root,owner,الادارة,الإدارة,ادارة,مشرف,المشرف,الدعم,الدعم الفني,الرسمي,فريق dzplay")
    NAME_BLOCKED_WORDS: str = ""  # extra words not allowed in names, comma separated ("word*" = starts with)
    SEARCH_PER_MINUTE: int = 20  # people searches per user per minute (anti-scraping)
    SEARCH_PER_DAY: int = 300
    SEARCH_MAX_RESULTS: int = 20  # per page
    SEARCH_MAX_PAGES: int = 3
    DIRECT_MSG_BEFORE_REPLY_LIMIT: int = 3  # messages before the recipient accepts or replies
    DIRECT_NEW_PER_DAY: int = 20  # new direct conversations a user may start per day
    TYPING_EVENTS_PER_MINUTE: int = 30
    WS_MAX_MESSAGE_BYTES: int = 16_384  # client → server WebSocket message size cap
    WS_MSGS_PER_10S: int = 60  # client → server WebSocket messages per connection per 10 s
    WS_PING_SECONDS: int = 25  # keep-alive interval; the session is re-checked at each one

    # --- Optional Firebase Cloud Messaging: new-message alerts in the Android app -------
    FCM_SERVICE_ACCOUNT_FILE: str = ""  # SECRET file (never in the repo): Firebase service-account JSON path
    FCM_PROJECT_ID: str = ""  # empty = read from the service-account file

    # --- V5: user media (idea images, ephemeral chat images) ------------------------------
    # Telegram is the file store; the server only relays. Two PRIVATE chats, both with the bot as admin:
    TELEGRAM_STORAGE_CHANNEL_ID: str = ""  # private channel holding every published file (e.g. -1001234567890)
    TELEGRAM_MODERATION_CHAT_ID: str = ""  # private group of moderators: each upload arrives with action buttons
    UPLOAD_TMP_DIR: str = "./upload-tmp"  # size-capped tmpfs in production; files live here seconds only
    UPLOAD_TMP_MAX_MB: int = 512  # new uploads wait (503) while the temp area holds more than this
    MEDIA_WORKER: str = "inline"  # inline (in the app process) | queue (separate media-worker container via Redis)
    MEDIA_WORKER_TIMEOUT: int = 600  # seconds the app waits for a queued job before giving up
    UPLOADS_PER_HOUR: int = 20  # any upload, per user
    UPLOAD_QUEUE_MAX: int = 30  # files waiting for processing; above this new uploads are asked to retry
    CAPTION_BLOCK_CATEGORIES: str = "sexual,threat,blackmail"  # word-filter categories that refuse a caption
    UPLOAD_IMAGE_TYPES: str = "jpeg,png,webp,heic"  # checked by magic bytes, never by the file name
    UPLOAD_IMAGE_MAX_MB: float = 12.0  # raw upload (the phone already compresses before sending)
    UPLOAD_IMAGE_MIN_SIDE: int = 64
    UPLOAD_IMAGE_MAX_SIDE: int = 12000  # also bounded by IMAGE_MAX_PIXELS (decompression bombs)
    DEVICE_IMAGE_MAX_SIDE: int = 1600  # the phone resizes to this before uploading
    DEVICE_IMAGE_QUALITY: float = 0.86
    # On-device + server NSFW check (NSFWJS MobileNetV2; same weights on both sides)
    DEVICE_NSFW_CHECK: bool = True
    SERVER_NSFW_CHECK: bool = True
    NSFW_BLOCK_THRESHOLD: float = 0.70  # P(porn) + P(hentai) at or above this is refused
    NSFW_SEXY_THRESHOLD: float = 0.92  # P(sexy) at or above this is refused (1.0 = never)
    # idea images
    IDEA_IMAGES_ENABLED: bool = True
    IDEA_IMAGE_LIMIT_PER_24H: int = 1
    IDEA_IMAGE_REQUIRE_APPROVAL: bool = False
    AVATAR_CHANGES_PER_DAY: int = 3  # V6 phase 3: new profile pictures per user in 24 h  # True: an idea with an image appears only after approval
    # ephemeral chat images
    CHAT_IMAGES_ENABLED: bool = True
    CHAT_IMAGE_TTL_AFTER_VIEW: int = 60  # seconds the picture stays visible once opened (V6 phase 6: 60)
    CHAT_IMAGE_UNOPENED_TTL: int = 24 * HOUR  # never opened: removed after this
    CHAT_IMAGE_PER_HOUR: int = 10
    CHAT_IMAGE_REPORT_GRACE: int = 600  # expired pictures stay reportable this long (storage copy only, never shown)
    CHAT_IMAGE_ARCHIVE: bool = False  # False: expired chat pictures are deleted everywhere (unless reported)
    CHAT_IMAGE_FLAG_SECURE: bool = True  # Android app blocks screenshots while a chat picture is open
    # reports on media
    REPORT_AUTO_HIDE_THRESHOLD: int = 3  # distinct reporters hide a picture/post until reviewed (0 = off)
    MEDIA_EVIDENCE_RETENTION_DAYS: int = 180  # reported / illegal media kept (Telegram) for review, then deleted
    MEDIA_TICK_SECONDS: int = 5  # expiry of chat pictures (0 = off; tests call media_items.tick)

    # --- V5: support tickets ----------------------------------------------------------
    SUPPORT_INBOX_EMAIL: str = ""  # tickets are e-mailed here (subject has the ticket number, Reply-To = the user)
    SUPPORT_TICKETS_PER_DAY: int = 3
    SUPPORT_MESSAGES_PER_HOUR: int = 10
    SUPPORT_MAX_LENGTH: int = 3000

    # --- V5: blue star (official trusted account; NOT identity verification) -------------
    VERIFY_ENABLED: bool = False  # V6: closed (not editable); the star comes with memberships in V6 phase 5
    VERIFY_MIN_POSTS: int = 5
    VERIFY_MIN_LIKES: int = 20
    VERIFY_MIN_ACCOUNT_AGE_DAYS: int = 14
    PAYMENT_MIN_AMOUNT: float = 0.0  # 0 = any amount (the wallet itself is set from the panel)

    # --- V6 phase 5: membership (features only — never any return; amounts in USDT) -----------------
    MEMBERSHIP_PRICE: float = 50.0
    MEMBERSHIP_REFUNDABLE: bool = True
    MEMBERSHIP_REFUND_WINDOW_DAYS: int = 7  # from the acceptance of the payment
    MEMBERSHIP_REFUND_FEE: float = 1.0  # kept from the refund (network fee)
    MEMBERSHIP_REQUESTS_PER_DAY: int = 3

    # --- V6 phase 5b: promotional rewards («أرباحي») — separate from membership, amounts in USDT -----
    REFERRAL_ENABLED: bool = True
    REFERRAL_REWARD: float = 5.0  # to the inviter when the invitee's membership is accepted
    REFERRAL_HOLD_DAYS: int = 14  # on hold this long; always longer than MEMBERSHIP_REFUND_WINDOW_DAYS
    REFERRAL_FLAG_PER_DAY: int = 3  # more invitations than this from one network in a day: admin review
    WITHDRAW_ENABLED: bool = True
    WITHDRAW_MIN: float = 10.0
    WITHDRAW_FEE: float = 1.0  # network fee, deducted from the amount sent (shown before confirming)
    WITHDRAW_REQUESTS_PER_DAY: int = 2
    WITHDRAW_NETWORKS: str = "TRC20,BEP20"

    # --- e-mail confirmation codes ------------------------------------------------------
    EMAIL_CODE_TTL: int = 30 * 60

    # --- V6 phase 2: market (Bybit public spot tickers; read-only, no account, no key) ----------
    # The server polls Bybit; phones only ever call /api/market. Never investment advice.
    MARKET_ENABLED: bool = True
    MARKET_BASE_URL: str = "https://api.bybit.com"  # or https://api.bytick.com (same API, other domain)
    MARKET_REFRESH_SECONDS: int = 60
    MARKET_KLINE_REFRESH_SECONDS: int = 600  # 24 h sparklines of the shown coins
    MARKET_TOP_N: int = 6  # gainers and losers each
    MARKET_MIN_TURNOVER_24H: float = 1_000_000.0  # USDT; thinner pairs are ignored
    MARKET_EXCLUDE: str = ""  # extra symbols to hide, comma separated (e.g. LUNAUSDT)
    MARKET_STABLECOINS: str = ("USDC,USDE,DAI,FDUSD,TUSD,BUSD,USDD,PYUSD,USDP,EURC,EURT,USD1,RLUSD,USDY,USDTB,"
                               "USTC,GUSD,LUSD,FRAX,USDQ,USDR,XUSD,BFUSD,AUSD,USDA,USDG")
    MARKET_TIMEOUT: float = 10.0
    MARKET_DISCLAIMER: str = "للاطلاع فقط وليست نصيحة استثمارية. الأسعار من Bybit وقد تتأخر قليلًا."

    # --- user protection: automatic flagging (app/services/moderation.py) ----
    MODERATION_ENABLED: bool = True  # scan new messages/comments; hits are queued for admin review
    MODERATION_EXTRA_WORDS: str = ""  # extra words/phrases, comma separated ("word*" = starts with)

    # --- derived / validation ----------------------------------------------
    secret_key_generated: bool = Field(default=False, exclude=True)
    _tunable_env: dict | None = PrivateAttr(default=None)  # .env values of the panel-tunable settings

    @field_validator("LOGIN_BLOCK_SCOPE")
    @classmethod
    def _scope(cls, v: str) -> str:
        if v not in ("ip_account", "ip"):
            raise ValueError("LOGIN_BLOCK_SCOPE must be 'ip_account' or 'ip'")
        return v

    @field_validator("LINK_POLICY")
    @classmethod
    def _links(cls, v: str) -> str:
        if v not in ("reject", "allow_plain"):
            raise ValueError("LINK_POLICY must be 'reject' or 'allow_plain'")
        return v

    @model_validator(mode="after")
    def _finalize(self) -> "Settings":
        if not self.SECRET_KEY:
            if self.ENV == "production":
                raise ValueError("SECRET_KEY must be set in production")
            self.SECRET_KEY = secrets.token_urlsafe(48)
            self.secret_key_generated = True
        if self.COOKIE_SECURE is None:
            self.COOKIE_SECURE = self.ENV == "production"
        return self

    # helpers ------------------------------------------------------------------
    @property
    def trusted_ips(self) -> set[str]:
        return {ip.strip() for ip in self.TRUSTED_IPS.split(",") if ip.strip()}

    @property
    def allowed_origins(self) -> set[str]:
        return {o.strip().rstrip("/") for o in self.ALLOWED_ORIGINS.split(",") if o.strip()}

    @property
    def google_enabled(self) -> bool:
        return bool(self.GOOGLE_CLIENT_ID)

    @property
    def android_cert_fingerprints(self) -> list[str]:
        return [f.strip().upper() for f in self.ANDROID_CERT_SHA256.split(",") if f.strip()]

    @property
    def admin_enabled(self) -> bool:
        return bool(self.ADMIN_PATH)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_ADMIN_CHAT_ID)

    @field_validator("MEDIA_WORKER")
    @classmethod
    def _media_worker(cls, v: str) -> str:
        if v not in ("inline", "queue"):
            raise ValueError("MEDIA_WORKER must be inline or queue")
        return v

    @property
    def image_types(self) -> set[str]:
        return {t.strip().lower() for t in self.UPLOAD_IMAGE_TYPES.split(",") if t.strip()}

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.SMTP_HOST and self.SMTP_FROM)

    @field_validator("ADMIN_PATH")
    @classmethod
    def _admin_path(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v:
            return ""
        import re as _re

        if not _re.fullmatch(r"/[A-Za-z0-9_-]{6,64}", v) or v.split("/")[1] in RESERVED_PATHS:
            raise ValueError("ADMIN_PATH must look like /panel-x7f3k9q2 (6-64 letters, digits, - or _)")
        return v

    @field_validator("SMTP_SECURITY")
    @classmethod
    def _smtp_security(cls, v: str) -> str:
        if v not in ("starttls", "ssl", "none"):
            raise ValueError("SMTP_SECURITY must be starttls, ssl or none")
        return v

    @property
    def fcm_enabled(self) -> bool:
        import os

        return bool(self.FCM_SERVICE_ACCOUNT_FILE) and os.path.isfile(self.FCM_SERVICE_ACCOUNT_FILE)

    @property
    def push_enabled(self) -> bool:
        return bool(self.VAPID_PUBLIC_KEY and self.VAPID_PRIVATE_KEY)


@lru_cache
def get_settings() -> Settings:
    return Settings()

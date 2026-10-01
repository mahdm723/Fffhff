"""Central configuration.

Every tunable value (limits, TTLs, matching rules, security knobs) lives here and
is read from environment variables or a `.env` file — nothing is hard-coded in
the business logic. See `.env.example` for documentation of each value.
"""

from __future__ import annotations

import secrets
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

HOUR = 3600
DAY = 24 * HOUR

# Rules the matcher always applies, whatever MATCHING_RULES says.
MANDATORY_MATCHING_RULES = ("not_self", "active_status", "not_blocked")
OPTIONAL_MATCHING_RULES = ("no_open_conversation", "not_recent_partner", "inbound_capacity")


# Bump when the privacy policy changes in a way users must be told about; users
# who acknowledged an older version see the new notice once (see /api/me).
PRIVACY_VERSION = 2


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- application -------------------------------------------------------
    APP_NAME: str = "DZPLAY"
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
    MAX_NEW_CONVERSATIONS_PER_HOUR: int = 5
    MAX_NEW_CONVERSATIONS_PER_DAY: int = 20
    MAX_CONSECUTIVE_MESSAGES: int = 8  # in a conversation, before the other side replies
    DUPLICATE_MESSAGE_WINDOW: int = 10 * 60  # identical anonymous messages are rejected
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

    # --- matching -----------------------------------------------------------
    MATCHING_RULES: str = ",".join(OPTIONAL_MATCHING_RULES)
    MATCH_EXCLUDE_RECENT_PARTNERS: int = 5
    MATCH_MAX_INBOUND_NEW_PER_DAY: int = 10
    MATCH_CANDIDATE_POOL: int = 50
    MATCH_RECENT_ACTIVITY_BOOST: float = 2.0  # weight boost for users active in the last day

    # --- temporary storage (TTL) -------------------------------------------
    MESSAGE_TTL: int = 7 * DAY  # max time a message stays on the server
    MESSAGE_TTL_AFTER_READ: int = 1 * DAY  # shortened once the recipient reads it
    CONVERSATION_IDLE_TTL: int = 14 * DAY  # conversation removed after this much inactivity
    CLEANUP_INTERVAL: int = 300
    SECURITY_EVENT_RETENTION: int = 30 * DAY
    REPORT_RETENTION: int = 90 * DAY

    # --- web push (optional) -----------------------------------------------
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_SUBJECT: str = "mailto:admin@example.com"

    # --- Android app (Trusted Web Activity) -------------------------------------
    # Digital Asset Links: lets the DZPLAY Android app open this site full screen.
    ANDROID_APP_PACKAGE: str = "io.dzplay.app"
    ANDROID_CERT_SHA256: str = (  # public fingerprint(s) of the APK signing key, comma separated
        "46:15:BE:65:23:30:C9:0A:C6:2C:C3:2C:E5:6B:0C:C0:CB:03:2B:8B:37:E7:13:86:0C:66:D0:A0:FA:EC:79:BF"
    )

    # --- admin ---------------------------------------------------------------
    ADMIN_API_TOKEN: str = ""  # empty disables the admin API

    # --- user protection: automatic flagging (app/services/moderation.py) ----
    MODERATION_ENABLED: bool = True  # scan new messages/comments; hits are queued for admin review
    MODERATION_EXTRA_WORDS: str = ""  # extra words/phrases, comma separated ("word*" = starts with)

    # --- derived / validation ----------------------------------------------
    secret_key_generated: bool = Field(default=False, exclude=True)

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
    def matching_rules(self) -> tuple[str, ...]:
        chosen = [r.strip() for r in self.MATCHING_RULES.split(",") if r.strip()]
        unknown = set(chosen) - set(OPTIONAL_MATCHING_RULES)
        if unknown:
            raise ValueError(f"Unknown MATCHING_RULES: {', '.join(sorted(unknown))}")
        return MANDATORY_MATCHING_RULES + tuple(chosen)

    @property
    def google_enabled(self) -> bool:
        return bool(self.GOOGLE_CLIENT_ID)

    @property
    def android_cert_fingerprints(self) -> list[str]:
        return [f.strip().upper() for f in self.ANDROID_CERT_SHA256.split(",") if f.strip()]

    @property
    def push_enabled(self) -> bool:
        return bool(self.VAPID_PUBLIC_KEY and self.VAPID_PRIVATE_KEY)


@lru_cache
def get_settings() -> Settings:
    return Settings()

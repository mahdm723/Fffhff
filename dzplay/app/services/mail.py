"""V6 phase 7: outgoing e-mail through two mailboxes.

* system  — codes (recovery, withdrawal, refund, red envelope), membership, rewards and withdrawals, prizes,
            security notices. Ends with the "automatic message, do not reply" footer; Reply-To = the support address.
* support — support tickets and their replies. Reply-To = the official support address (or the user, for mail
            sent to the support inbox).

A message is prepared inside a database session (settings, text) and delivered outside it, so no
transaction stays open while the SMTP server answers. A failed delivery is logged (error type only) and
reported to the admin in Telegram — at most once per 30 minutes per mailbox.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from email.utils import parseaddr

from sqlalchemy.orm import Session

from app.config import Settings
from app.services import mail_templates, mailer, runtime_config
from app.services.mailer import MailError

log = logging.getLogger("dzplay.mail")

LABELS = {"system": "بريد النظام", "support": "بريد الدعم"}
ALERT_EVERY = 1800
_STATE = None


def install(state) -> None:
    """Called once by the app state: failure alerts go to the admin chat through its bot."""
    global _STATE
    _STATE = state


@dataclass
class Outgoing:
    profile: str
    settings: Settings  # effective settings of the mailbox
    to: str
    subject: str
    body: str
    reply_to: str | None
    html: str | None


def support_address(db: Session, settings: Settings) -> str:
    """The official support address shown to users: the support mailbox's sender, else SUPPORT_INBOX_EMAIL."""
    cfg = runtime_config.smtp_config(db, settings, "support")
    if cfg["source"] != "none" and not cfg["inherited"]:
        addr = parseaddr(cfg["from"])[1]
        if addr:
            return addr
    return settings.SUPPORT_INBOX_EMAIL.strip()


def _html(app_name: str, text: str, highlight: str | None) -> str:
    """A simple RTL HTML version of the text (everything escaped; the code, if any, shown large)."""
    body = html.escape(text)
    if highlight:
        code = html.escape(highlight)
        body = body.replace(code, '<span style="display:inline-block;font-size:28px;letter-spacing:6px;font-weight:bold;'
                                  'direction:ltr;background:#f5f2ec;border-radius:12px;padding:10px 18px">' + code + "</span>", 1)
    body = body.replace("\n", "<br>")
    name = html.escape(app_name)
    return (f'<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><title>{name}</title></head>'
            '<body style="margin:0;background:#f5f2ec;font-family:Tahoma,Arial,sans-serif;color:#1a1c2a">'
            '<div style="max-width:480px;margin:24px auto;background:#fff;border-radius:18px;padding:28px 24px;'
            f'font-size:15px;line-height:1.8"><div style="font-size:20px;font-weight:bold;direction:ltr;text-align:right">{name}</div>'
            f'<div style="padding-top:16px">{body}</div></div></body></html>')


def prepare(db: Session, settings: Settings, profile: str, to: str, key: str, values: dict | None = None, *,
            reply_to: str | None = None, highlight: str | None = None) -> Outgoing | None:
    """The message ready to send, or None when this mailbox is not set up (nothing to send it with)."""
    eff = runtime_config.effective_settings(db, settings, profile)
    if not eff.smtp_enabled:
        return None
    subject, body = mail_templates.render(db, settings, key, values)
    support = support_address(db, settings)
    if profile == "system":
        _s, footer = mail_templates.render(db, settings, "email.footer",
                                           {"support_line": f" أو اكتب إلى {support}" if support else ""})
        body = f"{body}\n\n—\n{footer}"
    if reply_to is None and support:
        reply_to = support
    return Outgoing(profile, eff, to, subject, body, reply_to,
                    _html(settings.APP_NAME, body, highlight) if highlight else None)


def deliver(out: Outgoing) -> None:
    """Send it. Raises MailError; the admin is alerted (rate limited) when the server refuses or is unreachable."""
    try:
        mailer.send_text(out.settings, out.to, out.subject, out.body, reply_to=out.reply_to, html_body=out.html)
    except MailError as exc:
        failed(out.profile, exc)
        raise


def send(db: Session, settings: Settings, profile: str, to: str, key: str, values: dict | None = None, **kw) -> None:
    """prepare + deliver (raises MailError when the mailbox is not set up or the delivery fails)."""
    out = prepare(db, settings, profile, to, key, values, **kw)
    if out is None:
        raise MailError("SMTP is not configured")
    deliver(out)


def failed(profile: str, exc: Exception) -> None:
    log.warning("mail (%s) not sent: %s", profile, type(exc).__name__ if not isinstance(exc, MailError) else exc)
    state = _STATE
    if state is None or getattr(state, "bot", None) is None:
        return
    from app.services.rate_limit import Limit

    try:
        if not state.limiter.check_and_hit([Limit(f"mail_alert:{profile}", 1, ALERT_EVERY)]).allowed:
            return
        state.bot.say(f"⚠️ تعذّر إرسال بريد ({LABELS.get(profile, profile)}): {exc}.\n"
                      "تحقق من الإعداد: لوحة القيادة ← الأمان والنظام ← البريد ← «إرسال تجربة».")
    except Exception as alert_exc:  # noqa: BLE001 - an alert must never break the caller
        log.warning("mail alert not sent: %s", type(alert_exc).__name__)

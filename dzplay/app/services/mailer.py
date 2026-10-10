"""Outgoing e-mail over SMTP (low level). V6 phase 7: the texts, the two mailboxes (system / support) and the
failure alerts live in services/mail.py; this module only builds and delivers a message.

The message contains the code and nothing else about the account. SMTP
credentials (from .env or the admin panel, sealed in the database) are never logged.
"""

from __future__ import annotations

import html
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

from app.config import Settings


class MailError(Exception):
    pass


def _reset_bodies(app_name: str, code: str, hours: int) -> tuple[str, str]:
    text = (
        f"رمز استعادة حسابك في {app_name}:\n\n"
        f"    {code}\n\n"
        f"اكتب هذا الرمز في التطبيق لاختيار كلمة مرور جديدة. الرمز صالح {hours} ساعة ولمرة واحدة.\n"
        "إذا لم تطلب استعادة حسابك فتجاهل هذه الرسالة؛ كلمة مرورك الحالية لم تتغير.\n\n"
        f"— فريق {app_name}"
    )
    c = html.escape(code)
    name = html.escape(app_name)
    body = f"""<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{name}</title></head>
<body style="margin:0;background:#f5f2ec;font-family:Tahoma,Arial,sans-serif;color:#1a1c2a">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f2ec;padding:24px 12px">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;background:#ffffff;border-radius:18px;padding:28px 24px">
        <tr><td style="font-size:22px;font-weight:bold;direction:ltr;text-align:right">dzplay<span style="color:#e5466f">.</span></td></tr>
        <tr><td style="padding-top:18px;font-size:16px;line-height:1.7">رمز استعادة حسابك:</td></tr>
        <tr><td align="center" style="padding:16px 0">
          <div style="display:inline-block;font-size:30px;letter-spacing:6px;font-weight:bold;direction:ltr;background:#f5f2ec;border-radius:12px;padding:12px 20px">{c}</div>
        </td></tr>
        <tr><td style="font-size:14px;line-height:1.8;color:#4a4e63">
          اكتب هذا الرمز في التطبيق لاختيار كلمة مرور جديدة. الرمز صالح {hours} ساعة ولمرة واحدة.<br>
          إذا لم تطلب استعادة حسابك فتجاهل هذه الرسالة؛ كلمة مرورك الحالية لم تتغير.
        </td></tr>
        <tr><td style="padding-top:20px;font-size:12px;color:#868a9c">— فريق {name}</td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    return text, body


def _message(settings: Settings, to_email: str, subject: str) -> EmailMessage:
    msg = EmailMessage()
    from_name, from_addr = parseaddr(settings.SMTP_FROM)
    msg["From"] = formataddr((from_name or settings.APP_NAME, from_addr))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1] if "@" in from_addr else None)
    return msg


def send_reset_code(settings: Settings, to_email: str, code: str) -> None:
    if not settings.smtp_enabled:
        raise MailError("SMTP is not configured")
    hours = max(1, settings.RESET_CODE_TTL // 3600)
    text, body = _reset_bodies(settings.APP_NAME, code, hours)
    msg = _message(settings, to_email, f"رمز استعادة حساب {settings.APP_NAME}")
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    _deliver(settings, msg)


def send_text(settings: Settings, to_email: str, subject: str, text: str, reply_to: str | None = None,
              html_body: str | None = None) -> None:
    """A plain-text message (with an optional HTML alternative)."""
    if not settings.smtp_enabled:
        raise MailError("SMTP is not configured")
    msg = _message(settings, to_email, subject[:200])
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    _deliver(settings, msg)


def send_test(settings: Settings, to_email: str) -> None:
    """A short message to check the SMTP settings from the admin panel."""
    if not settings.smtp_enabled:
        raise MailError("SMTP is not configured")
    msg = _message(settings, to_email, f"{settings.APP_NAME}: بريد تجربة")
    msg.set_content(f"إعدادات البريد في {settings.APP_NAME} تعمل. ستصل رموز استعادة الحسابات بهذه الطريقة.")
    _deliver(settings, msg)


def _deliver(settings: Settings, msg: EmailMessage) -> None:
    try:
        if settings.SMTP_SECURITY == "ssl":
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT)
        with server:
            if settings.SMTP_SECURITY == "starttls":
                server.starttls(context=ssl.create_default_context())
            if settings.SMTP_USERNAME:
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        # Only the exception type and SMTP code: never credentials or the message itself.
        code_part = getattr(exc, "smtp_code", None)
        raise MailError(f"{type(exc).__name__}{f' {code_part}' if code_part else ''}") from None

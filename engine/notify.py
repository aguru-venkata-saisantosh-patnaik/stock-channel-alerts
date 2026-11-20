"""Free alert channels: Gmail (SMTP + app password), Telegram bot, WhatsApp via CallMeBot.

Each channel turns itself off when its environment variables are missing.
    python -m engine.notify --test     sends a test message to every configured channel
"""
from __future__ import annotations

import html
import os
import smtplib
import sys
from email.message import EmailMessage

import requests


def _env(*names: str) -> list[str] | None:
    vals = [os.environ.get(n, "").strip() for n in names]
    return vals if all(vals) else None


def configured() -> list[str]:
    out = []
    if _env("GMAIL_USER", "GMAIL_APP_PASSWORD"):
        out.append("email")
    if _env("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        out.append("telegram")
    if _env("CALLMEBOT_PHONE", "CALLMEBOT_APIKEY"):
        out.append("whatsapp")
    return out


def email(subject: str, text: str, html_body: str | None = None) -> None:
    creds = _env("GMAIL_USER", "GMAIL_APP_PASSWORD")
    if not creds:
        return
    user, password = creds
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = os.environ.get("ALERT_EMAIL_TO", "").strip() or user
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)


def telegram(text_html: str) -> None:
    creds = _env("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    if not creds:
        return
    token, chat_id = creds
    # Telegram caps a message at 4096 chars: split on blank lines
    chunks, cur = [], ""
    for part in text_html.split("\n\n"):
        if len(cur) + len(part) + 2 > 3900 and cur:
            chunks.append(cur)
            cur = ""
        cur += part + "\n\n"
    if cur.strip():
        chunks.append(cur)
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        r.raise_for_status()


def whatsapp(text: str) -> None:
    creds = _env("CALLMEBOT_PHONE", "CALLMEBOT_APIKEY")
    if not creds:
        return
    phone, apikey = creds
    r = requests.get(
        "https://api.callmebot.com/whatsapp.php",
        params={"phone": phone, "text": text[:1500], "apikey": apikey},
        timeout=60,
    )
    r.raise_for_status()


def send_all(subject: str, short_text: str, long_text: str, long_html: str, tg_html: str) -> list[str]:
    """Send to every configured channel; return a list of failures (never raises)."""
    failures = []
    for name, fn in (
        ("email", lambda: email(subject, long_text, long_html)),
        ("telegram", lambda: telegram(tg_html)),
        ("whatsapp", lambda: whatsapp(short_text)),
    ):
        if name not in configured():
            continue
        try:
            fn()
        except Exception as e:
            failures.append(f"{name}: {type(e).__name__}: {e}")
    return failures


if __name__ == "__main__":
    if "--test" not in sys.argv:
        sys.exit("usage: python -m engine.notify --test")
    chans = configured()
    print("configured channels:", chans or "none")
    msg = "✅ Std-dev channel alerts: test message. If you can read this, this channel works."
    fails = send_all(
        "Std-dev channel alerts: test",
        msg,
        msg,
        f"<p>{html.escape(msg)}</p>",
        html.escape(msg),
    )
    for f in fails:
        print("FAILED", f)
    sys.exit(1 if fails or not chans else 0)

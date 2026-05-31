"""
Email-репорт через Resend API.

Если RESEND_API_KEY не задан — фолбэк в stdout (полезно для отладки).
Если EMAIL_FROM не подтверждён в Resend — используется EMAIL_FROM_FALLBACK
(обычно `onboarding@resend.dev` — этот домен Resend разрешает по умолчанию).
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("send_email")


def send_report(subject: str, html: str, to: str | None = None) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("EMAIL_FROM", "").strip()
    fallback = os.environ.get("EMAIL_FROM_FALLBACK", "onboarding@resend.dev").strip()
    recipient = to or os.environ.get("EMAIL_TO", "").strip()

    if not recipient:
        log.warning("EMAIL_TO не задан — пропускаю отправку")
        print(f"\n=== EMAIL (skipped, no recipient) ===\nSubject: {subject}\n{html}\n=====================================\n")
        return False

    if not api_key:
        log.warning("RESEND_API_KEY не задан — печатаю письмо в stdout")
        print(f"\n=== EMAIL (RESEND_API_KEY missing) ===\nTo: {recipient}\nSubject: {subject}\n{html}\n========================================\n")
        return False

    payload = {
        "from": sender or fallback,
        "to": [recipient],
        "subject": subject,
        "html": html,
    }
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )
    except requests.RequestException as e:
        log.error("Resend network error: %s", e)
        return False

    if r.status_code in (200, 201):
        log.info("Email отправлен: %s", subject)
        return True

    # частая причина — домен EMAIL_FROM не верифицирован → пробуем fallback
    if r.status_code in (403, 422) and sender and sender != fallback:
        log.warning("Resend отклонил sender=%s (status=%d), пробую fallback=%s",
                    sender, r.status_code, fallback)
        payload["from"] = fallback
        r = requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )
        if r.status_code in (200, 201):
            log.info("Email отправлен через fallback: %s", subject)
            return True

    log.error("Resend failed: %d %s", r.status_code, r.text[:300])
    return False

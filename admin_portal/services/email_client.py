"""Convenience email sender for the super-admins."""
import logging
from typing import Iterable

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)


def send_admin_email(
    subject: str,
    text_body: str,
    html_body: str | None = None,
    recipients: Iterable[str] | None = None,
) -> bool:
    if not getattr(settings, "EMAIL_HOST_USER", ""):
        logger.info("EMAIL_HOST_USER not configured; skipping email: %s", subject)
        return False
    to = list(recipients) if recipients else list(getattr(settings, "SUPERADMIN_EMAILS", []))
    if not to:
        return False
    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", settings.EMAIL_HOST_USER),
            to=to,
        )
        if html_body:
            msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
        return True
    except Exception as exc:  # pragma: no cover - SMTP errors
        logger.exception("Email send failed: %s", exc)
        return False

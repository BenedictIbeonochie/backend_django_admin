"""Email + Slack notifications to the platform super-admins."""
from __future__ import annotations

import logging
from typing import Iterable

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def _admin_emails() -> list[str]:
    return list(getattr(settings, "SUPERADMIN_EMAILS", []))


def _slack_token_ok() -> bool:
    token = getattr(settings, "SLACK_BOT_TOKEN", "")
    return bool(token) and "REPLACE" not in token.upper()


def _email_ok() -> bool:
    return bool(getattr(settings, "EMAIL_HOST_USER", "")) \
        and "REPLACE" not in getattr(settings, "EMAIL_HOST_PASSWORD", "").upper()


def notify_flag(review, flag) -> dict:
    """Notify super-admins of a new AI flag. Returns delivery status dict."""
    subject = f"[Aqua Admin] {flag.severity.upper()} flag on {review.subject_type} {review.subject_display_name or review.subject_user_email}"
    body = (
        f"AI raised a {flag.severity} flag on a {review.subject_type} signup.\n\n"
        f"Subject: {review.subject_display_name or review.subject_user_email}\n"
        f"Decision: {review.decision} (confidence {review.confidence:.2f})\n"
        f"Reason: {flag.reason}\n\n"
        f"Recommended solution: {flag.recommended_solution}\n"
        f"Applied solution: {flag.applied_solution or '(none yet)'}\n\n"
        f"Open review: /admin-portal/reviews/{review.id}/\n"
    )
    delivered_email = _send_email(subject, body, _admin_emails())
    delivered_slack = _send_slack(
        f"*{flag.severity.upper()}* flag on `{review.subject_type}` "
        f"_{review.subject_display_name or review.subject_user_email}_\n"
        f"> {flag.reason}\n"
        f"_Recommended:_ {flag.recommended_solution}"
    )
    return {"email": delivered_email, "slack": delivered_slack, "recipients": _admin_emails()}


def notify_daily_report(report) -> dict:
    subject = f"[Aqua Admin] Daily AI review report — {report.report_date}"
    body = (
        f"AI auto-review summary for {report.report_date}\n\n"
        f"Approved : {report.approved_count}\n"
        f"Rejected : {report.rejected_count}\n"
        f"Flagged  : {report.flagged_count}\n"
        f"Pending  : {report.pending_count}\n\n"
        f"Breeders   reviewed: {report.breeder_count}\n"
        f"Consultants reviewed: {report.consultant_count}\n\n"
        f"{report.summary}\n\n"
        f"Open full report: /admin-portal/reports/{report.id}/\n"
    )
    delivered_email = _send_email(subject, body, _admin_emails())
    delivered_slack = _send_slack(
        f":bar_chart: Daily AI review for *{report.report_date}*: "
        f"{report.approved_count} approved, {report.rejected_count} rejected, "
        f"{report.flagged_count} flagged, {report.pending_count} pending."
    )
    return {"email": delivered_email, "slack": delivered_slack}


def notify_invite(invite, accept_url: str) -> dict:
    subject = "[Aqua Admin] You have been invited to the control plane"
    body = (
        f"{invite.created_by.email} has invited you to join the Aqua AI Admin control plane.\n\n"
        f"Accept the invite (expires {invite.expires_at:%Y-%m-%d %H:%M UTC}):\n  {accept_url}\n\n"
        f"If you were not expecting this, ignore this email.\n"
    )
    return {"email": _send_email(subject, body, [invite.email])}


# ---------------------------------------------------------------------------

def _send_email(subject: str, body: str, recipients: Iterable[str]) -> bool:
    recipients = [r for r in recipients if r]
    if not recipients:
        return False
    if not _email_ok():
        logger.warning("SMTP not configured; skipping email %r → %s", subject, recipients)
        return False
    try:
        send_mail(
            subject,
            body,
            getattr(settings, "DEFAULT_FROM_EMAIL", "admin@humara.io"),
            recipients,
            fail_silently=False,
        )
        return True
    except Exception:
        logger.exception("Email send failed")
        return False


def _send_slack(text: str) -> bool:
    if not _slack_token_ok():
        logger.warning("Slack not configured; skipping message")
        return False
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        client = WebClient(token=settings.SLACK_BOT_TOKEN)
        client.chat_postMessage(channel=settings.SLACK_CHANNEL, text=text)
        return True
    except SlackApiError:
        logger.exception("Slack send failed")
        return False
    except Exception:
        logger.exception("Slack client error")
        return False

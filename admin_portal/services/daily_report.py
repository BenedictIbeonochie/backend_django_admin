"""Build and deliver the end-of-day digest."""
import logging
from datetime import date, datetime, time, timedelta
from typing import Optional

from django.conf import settings
from django.db.models import Count, Q
from django.template.loader import render_to_string
from django.utils import timezone

from ..models import AIAccountReview, DailyReport
from . import email_client, openai_client, slack_client

logger = logging.getLogger(__name__)


def _bounds(report_date: date):
    start = datetime.combine(report_date, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def build_and_deliver_daily_report(report_date: Optional[date] = None) -> DailyReport:
    if report_date is None:
        report_date = timezone.now().date()
    start, end = _bounds(report_date)

    reviews = AIAccountReview.objects.filter(decided_at__gte=start, decided_at__lt=end)

    counts = reviews.aggregate(
        approved_count=Count("id", filter=Q(decision="approved")),
        rejected_count=Count("id", filter=Q(decision="rejected")),
        flagged_count=Count("id", filter=Q(decision="flagged")),
        pending_count=Count("id", filter=Q(decision="pending")),
        breeder_count=Count("id", filter=Q(subject_type="breeder")),
        consultant_count=Count("id", filter=Q(subject_type="consultant")),
    )

    decisions_payload = [
        {
            "id": str(r.id),
            "subject_type": r.subject_type,
            "display_name": r.subject_display_name,
            "email": r.subject_user_email,
            "decision": r.decision,
            "confidence": r.confidence,
            "rationale": r.rationale,
        }
        for r in reviews[:200]
    ]
    counts["total_reviewed"] = (
        counts["approved_count"] + counts["rejected_count"] + counts["flagged_count"]
    )
    summary = openai_client.summarise_day(counts, decisions_payload)

    report, _ = DailyReport.objects.update_or_create(
        report_date=report_date,
        defaults={
            **counts,
            "summary": summary,
            "details": {"decisions": decisions_payload},
        },
    )

    super_admins = getattr(settings, "SUPERADMIN_EMAILS", [])
    subject = f"[Aqua AI] Daily admin digest — {report_date.isoformat()}"
    text_body = (
        f"{summary}\n\n"
        f"Approved : {report.approved_count}\n"
        f"Rejected : {report.rejected_count}\n"
        f"Flagged  : {report.flagged_count}\n"
        f"Breeders : {report.breeder_count}\n"
        f"Consults : {report.consultant_count}\n"
        f"Open the control plane for full per-account drill-downs.\n"
    )
    html_body = render_to_string(
        "admin_portal/email/daily_report.html",
        {"report": report, "decisions": decisions_payload, "summary": summary},
    )
    report.delivered_email = email_client.send_admin_email(subject, text_body, html_body, super_admins)
    report.delivered_slack = slack_client.post_message(
        text=subject,
        blocks=[
            {"type": "header", "text": {"type": "plain_text", "text": subject[:150]}},
            {"type": "section", "text": {"type": "mrkdwn", "text": summary or "_No summary_"}},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Approved:* {report.approved_count}    "
                        f"*Rejected:* {report.rejected_count}    "
                        f"*Flagged:* {report.flagged_count}"
                    ),
                },
            },
        ],
    )
    report.save(update_fields=["delivered_email", "delivered_slack"])
    return report

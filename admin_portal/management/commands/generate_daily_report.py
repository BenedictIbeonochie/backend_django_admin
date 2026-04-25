from datetime import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from admin_portal.services import build_and_deliver_daily_report


class Command(BaseCommand):
    help = "Build today's analytics digest and email/Slack it to the super-admins."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="YYYY-MM-DD; defaults to today (UTC).")

    def handle(self, *args, **options):
        report_date = None
        if options.get("date"):
            report_date = datetime.strptime(options["date"], "%Y-%m-%d").date()
        report = build_and_deliver_daily_report(report_date)
        self.stdout.write(
            self.style.SUCCESS(
                f"Daily report for {report.report_date} — "
                f"approved={report.approved_count} rejected={report.rejected_count} "
                f"flagged={report.flagged_count}"
            )
        )

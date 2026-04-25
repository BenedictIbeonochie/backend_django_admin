from django.core.management.base import BaseCommand

from admin_portal.services import review_pending


class Command(BaseCommand):
    help = (
        "Find new breeder/consultant profiles in the main backend that haven't been "
        "reviewed yet, run them through GPT-4 and apply / notify accordingly."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        results = review_pending(limit=options["limit"])
        self.stdout.write(self.style.SUCCESS(f"Reviewed {len(results)} profile(s)."))
        for r in results:
            self.stdout.write(
                f"  {r.subject_type:11s} {r.subject_id} → {r.decision:8s} ({r.confidence:.2f})"
            )

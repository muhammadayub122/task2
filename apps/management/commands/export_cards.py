import csv
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.models import Card
from apps.utils import build_card_export_rows, build_cards_xlsx_bytes, filter_cards_queryset


class Command(BaseCommand):
    help = "Export cards with optional filters."

    def add_arguments(self, parser):
        parser.add_argument("--status")
        parser.add_argument("--card-number")
        parser.add_argument("--phone")
        parser.add_argument("--format", choices=("csv", "xlsx"))
        parser.add_argument("--value-style", choices=("formatted", "raw"), default="formatted")
        parser.add_argument("--output", default="cards_export.csv")

    def handle(self, *args, **options):
        queryset = filter_cards_queryset(
            Card.objects.all(),
            status=options.get("status"),
            card_number=options.get("card_number"),
            phone=options.get("phone"),
        )
        output = options["output"]
        export_format = options.get("format") or Path(output).suffix.lstrip(".").lower() or "csv"
        value_style = options["value_style"]
        if export_format == "xlsx":
            with open(output, "wb") as file:
                file.write(build_cards_xlsx_bytes(queryset, value_style=value_style))
        else:
            with open(output, "w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                for row in build_card_export_rows(queryset, value_style=value_style):
                    writer.writerow(row)
        self.stdout.write(self.style.SUCCESS(f"Exported {queryset.count()} cards to {output}"))

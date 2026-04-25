from django.core.management.base import BaseCommand

from apps.models import Card
from apps.utils import filter_cards_queryset, prepare_message, send_message


class Command(BaseCommand):
    help = "Send simulated messages to filtered cards."

    def add_arguments(self, parser):
        parser.add_argument("--status")
        parser.add_argument("--card-number")
        parser.add_argument("--phone")
        parser.add_argument("--lang", default="UZ")

    def handle(self, *args, **options):
        queryset = filter_cards_queryset(
            Card.objects.exclude(phone__isnull=True).exclude(phone=""),
            status=options.get("status"),
            card_number=options.get("card_number"),
            phone=options.get("phone"),
        )
        sent_count = 0
        for card in queryset:
            message = prepare_message(card.card_number, card.balance, lang=options["lang"])
            send_message(message)
            sent_count += 1
            self.stdout.write(f"Sent to {card.card_number}: {message}")
        self.stdout.write(self.style.SUCCESS(f"Simulated {sent_count} messages."))

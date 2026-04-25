from django.core.management.base import BaseCommand
from apps.models import Card
import csv


class Command(BaseCommand):
    help = "Export cards to CSV"

    def handle(self, *args, **kwargs):
        with open('cards.csv', 'w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(['id', 'name'])  # поля

            for card in Card.objects.all():
                writer.writerow([card.id, card.name])

        self.stdout.write(self.style.SUCCESS("Export done!"))
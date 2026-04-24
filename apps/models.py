from django.db import models

# Create your models here.
from django.db import models
from django.contrib.auth.models import AbstractUser
from decimal import Decimal


class StatusChoices(models.TextChoices):
    ACTIVE = "active", "Active"
    BLOCKED = "blocked", "Blocked"
    EXPIRED = "expired", "Expired"


class Card(models.Model):
    card_number = models.CharField(max_length=16, unique=True)
    phone = models.CharField(max_length=13)
    balance = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("0.00")
    )
    status = models.CharField(max_length=10, choices=StatusChoices.choices)
    expire_date = models.DateField()

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.card_number} ({self.phone})"


class User(AbstractUser):
    telegram_id = models.CharField(max_length=50, null=True, blank=True)
    phone_number = models.CharField(max_length=15, unique=True, null=True, blank=True)
    language = models.CharField(max_length=2, default="uz")
    date_of_birth = models.DateField(blank=True, null=True)
    def __str__(self):
        return self.username

class UserCard(models.Model):
    user = models.ForeignKey(User, related_name="cards", on_delete=models.CASCADE)
    card = models.ForeignKey(
        Card,
        on_delete=models.CASCADE,
        related_name="userscard",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return str(self.user.id)

class TransferState(models.TextChoices):
    CREATED = "created", "Created"
    CONFIRMED = "confirmed", "Confirmed"
    CANCELLED = "cancelled", "Cancelled"

class CurrencyChoices(models.IntegerChoices):
    UZS = 860, "UZS"
    RUB = 643, "RUB"
    USD = 840, "USD"

class Transfer(models.Model):
    ext_id = models.CharField(max_length=64, unique=True)
    sender_card_number = models.CharField(max_length=16)
    sender_card_expiry = models.CharField(max_length=5)
    eceiver_card_number = models.CharField(max_length=16)
    sender_phone = models.CharField(max_length=13, null=True, blank=True)
    receiver_phone = models.CharField(max_length=13, null=True, blank=True)
    sending_amount = models.DecimalField(max_digits=20, decimal_places=2)
    currency = models.IntegerField(choices=CurrencyChoices.choices)
    receiving_amount = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    state = models.CharField(max_length=10,choices=TransferState.choices,default=TransferState.CONFIRMED,)
    try_count = models.PositiveSmallIntegerField(default=0)
    otp = models.CharField(max_length=6, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.ext_id} [{self.state}]"


class Errors(models.Model):
    code = models.IntegerField(unique=True)
    en = models.CharField(max_length=255)
    ru = models.CharField(max_length=255)
    uz = models.CharField(max_length=255)

    def get_message(self, lang="uz"):
        """Tilga qarab sms yuboriladi  (default: uz)"""
        return getattr(self, lang, self.en)

    def __str__(self):
        return f"{self.code}: {self.en}"
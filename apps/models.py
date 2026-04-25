<<<<<<< Updated upstream
=======
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class CardStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"
    EXPIRED = "expired", "Expired"


class TransferState(models.TextChoices):
    CREATED = "created", "Created"
    CONFIRMED = "confirmed", "Confirmed"
    CANCELLED = "cancelled", "Cancelled"


class Card(models.Model):
    card_number = models.CharField(max_length=16, unique=True)
    expire = models.DateField(db_column="expire_date")
    phone = models.CharField(max_length=13, blank=True, null=True)
    status = models.CharField(max_length=10, choices=CardStatus.choices)
    balance = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0")),
            MaxValueValidator(Decimal("1200000000")),
        ],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["card_number"]

    def clean(self) -> None:
        from .utils import format_card, format_phone, parse_expire, validate_card

        errors: dict[str, str] = {}

        try:
            self.card_number = format_card(self.card_number)
            if not validate_card(self.card_number):
                errors["card_number"] = "Card number failed LUHN validation."
        except ValueError as exc:
            errors["card_number"] = str(exc)

        try:
            self.expire = parse_expire(self.expire)
        except ValueError as exc:
            errors["expire"] = str(exc)

        try:
            self.phone = format_phone(self.phone) if self.phone else None
        except ValueError as exc:
            errors["phone"] = str(exc)

        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return self.card_number


class Error(models.Model):
    code = models.IntegerField(unique=True)
    en = models.CharField(max_length=255)
    ru = models.CharField(max_length=255)
    uz = models.CharField(max_length=255)

    class Meta:
        db_table = "apps_errors"
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code}: {self.en}"

# Create your models here.
from django.db import models
from django.contrib.auth.models import AbstractUser
>>>>>>> Stashed changes
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class CardStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"
    EXPIRED = "expired", "Expired"


class TransferState(models.TextChoices):
    CREATED = "created", "Created"
    CONFIRMED = "confirmed", "Confirmed"
    CANCELLED = "cancelled", "Cancelled"


<<<<<<< Updated upstream
class Card(models.Model):
    card_number = models.CharField(max_length=16, unique=True)
    expire = models.DateField(db_column="expire_date")
    phone = models.CharField(max_length=13, blank=True, null=True)
    status = models.CharField(max_length=10, choices=CardStatus.choices)
    balance = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0")),
            MaxValueValidator(Decimal("1200000000")),
        ],
    )
=======
class Transfer(models.Model):
    ext_id = models.CharField(max_length=64, unique=True)
    sender_card_number = models.CharField(max_length=16)
    receiver_card_number = models.CharField(max_length=16, db_column="eceiver_card_number")
    sender_card_expiry = models.CharField(max_length=5)
    sender_phone = models.CharField(max_length=13, blank=True, null=True)
    receiver_phone = models.CharField(max_length=13, blank=True, null=True)
    sending_amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.IntegerField()
    receiving_amount = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    state = models.CharField(max_length=10, choices=TransferState.choices, default=TransferState.CREATED)
    try_count = models.PositiveSmallIntegerField(default=0)
    otp = models.CharField(max_length=6, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(blank=True, null=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.ext_id
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
>>>>>>> Stashed changes
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["card_number"]

    def clean(self) -> None:
        from .utils import format_card, format_phone, parse_expire, validate_card

        errors: dict[str, str] = {}

        try:
            self.card_number = format_card(self.card_number)
            if not validate_card(self.card_number):
                errors["card_number"] = "Card number failed LUHN validation."
        except ValueError as exc:
            errors["card_number"] = str(exc)

        try:
            self.expire = parse_expire(self.expire)
        except ValueError as exc:
            errors["expire"] = str(exc)

        try:
            self.phone = format_phone(self.phone) if self.phone else None
        except ValueError as exc:
            errors["phone"] = str(exc)

        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return self.card_number


class Error(models.Model):
    code = models.IntegerField(unique=True)
    en = models.CharField(max_length=255)
    ru = models.CharField(max_length=255)
    uz = models.CharField(max_length=255)

    class Meta:
        db_table = "apps_errors"
        ordering = ["code"]

<<<<<<< Updated upstream
    def __str__(self) -> str:
        return f"{self.code}: {self.en}"


class Transfer(models.Model):
    ext_id = models.CharField(max_length=64, unique=True)
    sender_card_number = models.CharField(max_length=16)
    receiver_card_number = models.CharField(max_length=16, db_column="eceiver_card_number")
    sender_card_expiry = models.CharField(max_length=5)
    sender_phone = models.CharField(max_length=13, blank=True, null=True)
    receiver_phone = models.CharField(max_length=13, blank=True, null=True)
    sending_amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.IntegerField()
    receiving_amount = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    state = models.CharField(max_length=10, choices=TransferState.choices, default=TransferState.CREATED)
    try_count = models.PositiveSmallIntegerField(default=0)
    otp = models.CharField(max_length=6, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(blank=True, null=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.ext_id
=======
    def __str__(self):
        return f"{self.code}: {self.en}"
>>>>>>> Stashed changes

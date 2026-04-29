import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
import uuid
from .models import Card, CardStatus, Error, Transfer, TransferState
from .utils import (
    build_card_export_rows,
    build_cards_xlsx_bytes,
    calculate_exchange,
    format_card,
    generate_otp,
    get_transfer_by_ext_id,
    normalize_card_row,
    parse_expire,
    parse_card_rows,
    prepare_message,
    send_sms_code,
    send_telegram_message,
    validate_card,
)

logger = logging.getLogger(__name__)

OTP_LIFETIME_MINUTES = 5
MAX_OTP_TRIES = 3

from django.core.cache import cache

OTP_CACHE_TIMEOUT = 300  # 5 minutes


@dataclass
class BusinessError(Exception):
    code: int
    message: str

    def __str__(self) -> str:
        return self.message


def send_otp_to_chat(params: dict) -> dict:
    chat_id = str(params.get("chat_id", "")).strip()
    if not chat_id:
        raise BusinessError(32706, "chat_id is required.")
    otp = generate_otp()
    cache_key = f"otp_{chat_id}"
    cache.set(cache_key, otp, timeout=OTP_CACHE_TIMEOUT)
    message = f"Your verification code: {otp}"
    result = send_telegram_message("", message, chat_id=int(chat_id))
    if not result.get("sent"):
        logger.error("Failed to send OTP telegram to chat_id %s. Check TELEGRAM_BOT_TOKEN.", chat_id)
        return {
            "sent": False,
            "chat_id": chat_id,
            "otp": otp,
            "warning": "Telegram message failed. Check TELEGRAM_BOT_TOKEN and chat_id.",
        }
    logger.info("OTP sent to chat_id %s: %s", chat_id, otp)
    return {"sent": True, "chat_id": chat_id, "otp": otp}


def get_error_message(code: int, lang: str = "en") -> str:
    defaults = {
        32700: "Ext id must be unique",
        32701: "Ext id already exists",
        32702: "Balance is not enough",
        32703: "SMS service is not bind",
        32704: "Card expiry is not valid",
        32705: "Card is not active",
        32706: "Unknown error occurred",
        32707: "Currency not allowed except 860, 643, 840",
        32708: "Amount is greater than allowed",
        32709: "Amount is small",
        32710: "OTP expired",
        32711: "Count of try is reached",
        32712: "OTP is wrong, left try count is 2",
        32713: "Method is not allowed",
        32714: "Method not found",
    }
    error = Error.objects.filter(code=code).first()
    if error and hasattr(error, lang):
        return getattr(error, lang)
    return defaults[code]


def ensure_ext_id_unique(ext_id: str) -> None:
    if Transfer.objects.filter(ext_id=ext_id).exists():
        raise BusinessError(32701, get_error_message(32701))


def get_card_or_raise(card_number: str) -> Card:
    normalized = format_card(card_number)
    if not validate_card(normalized):
        raise BusinessError(32706, "Card number is invalid.")
    try:
        return Card.objects.get(card_number=normalized)
    except Card.DoesNotExist as exc:
        raise BusinessError(32706, "Card does not exist.") from exc


def validate_transfer_amount(amount: Decimal) -> Decimal:
    amount = Decimal(amount).quantize(Decimal("0.01"))
    if amount <= 0:
        raise BusinessError(32709, get_error_message(32709))
    if amount > Decimal("1200000000"):
        raise BusinessError(32708, get_error_message(32708))
    return amount


def validate_sender_card(card: Card, sender_card_expiry: str, sending_amount: Decimal) -> None:
    if card.status != CardStatus.ACTIVE:
        raise BusinessError(32705, get_error_message(32705))
    if card.phone in (None, ""):
        raise BusinessError(32703, get_error_message(32703))
    if card.expire != parse_expire(sender_card_expiry):
        raise BusinessError(32704, get_error_message(32704))
    if card.balance < sending_amount:
        raise BusinessError(32702, get_error_message(32702))

def validate_currency(currency: int) -> int:
    if currency not in (860, 643, 840):
        raise BusinessError(32707, get_error_message(32707))
    return currency


@transaction.atomic
def create_transfer(params: dict) -> dict:
    ext_id = str(params.get("ext_id", f"tr-{uuid.uuid4()}")).strip()
    if not ext_id:
        raise BusinessError(32706, "ext_id is required.")

    ensure_ext_id_unique(ext_id)

    # Support both internal and legacy RPC parameter names
    sending_amount_raw = params.get("amount") or params.get("sending_amount")
    if sending_amount_raw is None:
        raise BusinessError(32706, "Parameter 'amount' is required.")
    amount = validate_transfer_amount(sending_amount_raw)

    currency_raw = params.get("currency")
    if currency_raw is None:
        raise BusinessError(32706, "Parameter 'currency' is required.")
    currency = validate_currency(int(currency_raw))

    sender_card_number_raw = params.get("from_card") or params.get("sender_card_number")
    if sender_card_number_raw is None:
        raise BusinessError(32706, "Parameter 'from_card' (sender card number) is required.")
    sender_card = get_card_or_raise(sender_card_number_raw)

    receiver_card_number_raw = params.get("to_card") or params.get("receiver_card_number")
    if receiver_card_number_raw is None:
        raise BusinessError(32706, "Parameter 'to_card' (receiver card number) is required.")

    if str(sender_card_number_raw).strip() == str(receiver_card_number_raw).strip():
        raise BusinessError(32706, "Sender and receiver cards must be different.")

    receiver_card = get_card_or_raise(receiver_card_number_raw)

    sender_card_expiry_raw = params.get("sender_card_expiry")
    if sender_card_expiry_raw is None:
        raise BusinessError(32706, "Parameter 'sender_card_expiry' is required.")
    validate_sender_card(sender_card, sender_card_expiry_raw, amount)

    chat_id = params.get("chat_id")
    if chat_id is not None:
        chat_id = str(chat_id).strip()

    user_otp = params.get("user_otp")
    auto_confirm = False
    if user_otp is not None and chat_id:
        user_otp = str(user_otp).strip()
        stored = cache.get(f"otp_{chat_id}")
        if stored and stored == user_otp:
            auto_confirm = True
        else:
            raise BusinessError(32712, "Incorrect OTP.")

    otp = generate_otp()
    sms_text = f"Tasdiqlash kodi: {otp}. (ID: {ext_id})"
    send_sms_code(sender_card.phone, sms_text)

    if chat_id and not auto_confirm:
        send_telegram_message(
            sender_card.phone or "",
            sms_text,
            chat_id=int(chat_id),
        )

    if auto_confirm:
        sender_card = Card.objects.select_for_update().get(card_number=sender_card.card_number)
        receiver_card = Card.objects.select_for_update().get(card_number=receiver_card.card_number)
        if sender_card.balance < amount:
            raise BusinessError(32702, get_error_message(32702))
        sender_card.balance -= amount
        receiver_card.balance += amount
        sender_card.save(update_fields=["balance"])
        receiver_card.save(update_fields=["balance"])
        transfer = Transfer.objects.create(
            ext_id=ext_id,
            sender_card_number=sender_card.card_number,
            receiver_card_number=receiver_card.card_number,
            sender_card_expiry=parse_expire(sender_card_expiry_raw).strftime("%m/%y"),
            sender_phone=sender_card.phone,
            receiver_phone=receiver_card.phone,
            sending_amount=amount,
            currency=currency,
            receiving_amount=calculate_exchange(amount, currency),
            state=TransferState.CONFIRMED,
            otp=otp,
            chat_id=chat_id,
            confirmed_at=timezone.now(),
        )
        message = prepare_message(sender_card.card_number, sender_card.balance)
        if chat_id:
            send_telegram_message(
                sender_card.phone or "",
                message,
                chat_id=int(chat_id),
            )
        logger.info("Transfer created and auto-confirmed: %s", transfer.ext_id)
        return {"ext_id": transfer.ext_id, "state": transfer.state, "confirmed": True}

    transfer = Transfer.objects.create(
        ext_id=ext_id,
        sender_card_number=sender_card.card_number,
        receiver_card_number=receiver_card.card_number,
        sender_card_expiry=parse_expire(sender_card_expiry_raw).strftime("%m/%y"),
        sender_phone=sender_card.phone,
        receiver_phone=receiver_card.phone,
        sending_amount=amount,
        currency=currency,
        receiving_amount=calculate_exchange(amount, currency),
        state=TransferState.CREATED,
        otp=otp,
        chat_id=chat_id,
    )
    logger.info("Transfer created: %s", transfer.ext_id)
    return {"ext_id": transfer.ext_id, "state": transfer.state, "otp_sent": True}

     
@transaction.atomic
def confirm_transfer(params: dict) -> dict:
    try:
        transfer = get_transfer_by_ext_id(str(params["ext_id"]).strip())
    except Transfer.DoesNotExist as exc:
        raise BusinessError(32706, get_error_message(32706)) from exc
    if transfer.state != TransferState.CREATED:
        return {"ext_id": transfer.ext_id, "state": transfer.state}
    if transfer.try_count >= MAX_OTP_TRIES:
        raise BusinessError(32711, get_error_message(32711))
    if transfer.created_at + timedelta(minutes=OTP_LIFETIME_MINUTES) < timezone.now():
        raise BusinessError(32710, get_error_message(32710))

    otp = str(params["otp"]).strip()
    if transfer.otp != otp:
        transfer.try_count += 1
        transfer.save(update_fields=["try_count", "updated_at"])
        left = MAX_OTP_TRIES - transfer.try_count
        if left <= 0:
            raise BusinessError(32711, get_error_message(32711))
        raise BusinessError(32712, f"Incorrect OTP. Attempts left: {left}")

    # Lock cards in a deterministic order (by card_number) to prevent deadlocks
    card_numbers = sorted([transfer.sender_card_number, transfer.receiver_card_number])
    locked_cards = {
        c.card_number: c 
        for c in Card.objects.select_for_update().filter(card_number__in=card_numbers)
    }

    sender_card = locked_cards.get(transfer.sender_card_number)
    receiver_card = locked_cards.get(transfer.receiver_card_number)

    if not sender_card or not receiver_card:
        raise BusinessError(32706, "One of the cards involved in the transfer no longer exists.")

    if sender_card.balance < transfer.sending_amount:
        raise BusinessError(32702, get_error_message(32702))

    sender_card.balance -= transfer.sending_amount
    receiver_card.balance += transfer.sending_amount
    sender_card.save(update_fields=["balance"])
    receiver_card.save(update_fields=["balance"])

    transfer.state = TransferState.CONFIRMED
    transfer.confirmed_at = timezone.now()
    transfer.save(update_fields=["state", "confirmed_at", "updated_at"])

    message = prepare_message(sender_card.card_number, sender_card.balance)
    if transfer.chat_id:
        send_telegram_message(
            sender_card.phone or "",
            message,
            chat_id=int(transfer.chat_id),
        )
    logger.info("Transfer confirmed: %s", transfer.ext_id)
    return {"ext_id": transfer.ext_id, "state": transfer.state}


@transaction.atomic
def cancel_transfer(params: dict) -> dict:
    try:
        transfer = get_transfer_by_ext_id(str(params["ext_id"]).strip())
    except Transfer.DoesNotExist as exc:
        raise BusinessError(32706, get_error_message(32706)) from exc
    if transfer.state == TransferState.CREATED:
        transfer.state = TransferState.CANCELLED
        transfer.cancelled_at = timezone.now()
        transfer.save(update_fields=["state", "cancelled_at", "updated_at"])
        logger.info("Transfer cancelled: %s", transfer.ext_id)
    return {"ext_id": transfer.ext_id, "state": transfer.state}


def transfer_state(params: dict) -> dict:
    try:
        transfer = get_transfer_by_ext_id(str(params["ext_id"]).strip())
    except Transfer.DoesNotExist as exc:
        raise BusinessError(32706, get_error_message(32706)) from exc
    return {"ext_id": transfer.ext_id, "state": transfer.state}


def transfer_history(params: dict) -> list[dict]:
    queryset = Transfer.objects.all()
    card_number = params.get("card_number")
    if card_number:
        normalized = format_card(card_number)
        queryset = queryset.filter(
            Q(sender_card_number=normalized) | Q(receiver_card_number=normalized)
        )
    status = params.get("status")
    if status:
        queryset = queryset.filter(state=str(status).strip().lower())
    start_date = params.get("start_date")
    if start_date:
        queryset = queryset.filter(created_at__date__gte=start_date)
    end_date = params.get("end_date")
    if end_date:
        queryset = queryset.filter(created_at__date__lte=end_date)
    result = []
    for item in queryset.order_by("-created_at"):
        result.append(
            {
                "ext_id": item.ext_id,
                "sending_amount": str(item.sending_amount),
                "state": item.state,
                "created_at": item.created_at.isoformat(),
            }
        )
    return result


def jsonrpc_success(request_id, result: dict | list) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def log_rpc_call(method: str, payload: dict) -> None:
    logger.info("RPC %s request: %s", method, json.dumps(payload, default=str))


def import_cards(file, file_format: str | None = None) -> dict[str, object]:
    selected_format = (file_format or "").lower() or file.name.rsplit(".", 1)[-1].lower()
    file_bytes = file.read()
    if selected_format == "json":
        rows = json.loads(file_bytes.decode("utf-8-sig"))
        if not isinstance(rows, list):
            raise ValueError("JSON file must contain a list of cards.")
    else:
        rows = parse_card_rows(file.name, file_bytes)

    created_count = 0
    updated_count = 0
    errors: list[str] = []
    for index, row in enumerate(rows, start=2):
        try:
            normalized = normalize_card_row(row)
            _, created = Card.objects.update_or_create(
                card_number=normalized["card_number"],
                defaults=normalized,
            )
            if created:
                created_count += 1
            else:
                updated_count += 1
        except Exception as exc:
            errors.append(f"Row {index}: {exc}")
    return {"created": created_count, "updated": updated_count, "errors": errors}


def export_cards(
    export_format: str,
    status: str | None = None,
    value_style: str = "formatted",
) -> HttpResponse:
    queryset = Card.objects.all()
    if status:
        queryset = queryset.filter(status=status)

    if export_format == "json":
        payload = [
            {
                "card_number": card.card_number,
                "expire": card.expire.strftime("%Y-%m"),
                "phone": card.phone or "",
                "status": card.status,
                "balance": format(card.balance, ".2f"),
            }
            for card in queryset
        ]
        response = HttpResponse(content_type="application/json")
        response["Content-Disposition"] = 'attachment; filename="cards_export.json"'
        response.write(json.dumps(payload, ensure_ascii=False, indent=2))
        return response

    if export_format == "xlsx":
        response = HttpResponse(
            build_cards_xlsx_bytes(queryset, value_style=value_style),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="cards_export.xlsx"'
        return response

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="cards_export.csv"'
    writer = csv.writer(response)
    for row in build_card_export_rows(queryset, value_style=value_style):
        writer.writerow(row)
    return response

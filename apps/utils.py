import csv
import io
import json
import logging
import random
import re
import urllib.request
import urllib.error
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from django.conf import settings
from django.db.models import QuerySet

from .models import Transfer

logger = logging.getLogger(__name__)

XLSX_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
EXCHANGE_RATES = {
    860: Decimal("1"),
    643: Decimal("145"),
    840: Decimal("12700"),
}
CARD_HEADERS = ["card_number", "expire", "phone", "status", "balance"]
HEADER_ALIASES = {
    "cardnumber": "card_number",
    "card_number": "card_number",
    "card": "card_number",
    "expire": "expire",
    "expiry": "expire",
    "expiredate": "expire",
    "expire_date": "expire",
    "phone": "phone",
    "phonenumber": "phone",
    "phone_number": "phone",
    "status": "status",
    "balance": "balance",
}


def digits_only(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def stringify_spreadsheet_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(value, "f").rstrip("0").rstrip(".")
    return str(value).strip()


def card_mask(card_number: str) -> str:
    digits = digits_only(card_number)
    if len(digits) < 8:
        return digits
    return f"{digits[:4]} **** **** {digits[-4:]}"


def phone_mask(phone: str) -> str:
    digits = digits_only(phone)
    if len(digits) < 7:
        return digits
    return f"+998 {digits[-9:-7]} *** ** {digits[-2:]}"


def format_card(raw_card: Any) -> str:
    raw_value = stringify_spreadsheet_value(raw_card)
    scientific_match = re.match(r"^\d+(?:\.\d+)?[eE][+-]?\d+$", raw_value)
    if scientific_match:
        try:
            raw_value = format(Decimal(raw_value), "f")
        except InvalidOperation:
            pass
    digits = digits_only(raw_value)
    if len(digits) != 16:
        raise ValueError("Card number must contain exactly 16 digits.")
    return digits


def display_card(card_number: str) -> str:
    digits = format_card(card_number)
    return " ".join(digits[index:index + 4] for index in range(0, 16, 4))


def format_phone(raw_phone: Any) -> str:
    digits = digits_only(raw_phone)
    if not digits:
        return ""
    if len(digits) == 7:
        digits = f"99{digits}"
    if len(digits) == 9:
        digits = f"998{digits}"
    if len(digits) == 12 and digits.startswith("998"):
        return digits
    raise ValueError("Phone number must contain 7, 9, or 12 digits.")


def display_phone(phone: str) -> str:
    formatted = format_phone(phone)
    return f"+{formatted[:3]} {formatted[3:5]} {formatted[5:8]} {formatted[8:10]} {formatted[10:12]}"


def parse_balance(raw_balance: Any) -> Decimal:
    cleaned = stringify_spreadsheet_value(raw_balance).replace(",", "").strip()
    if not cleaned:
        raise ValueError("Balance is required.")
    try:
        amount = Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("Balance format is invalid.") from exc
    if amount < 0 or amount > Decimal("1200000000"):
        raise ValueError("Balance must be between 0 and 1.2 billion UZS.")
    return amount


def parse_expire(raw_expire: Any) -> date:
    if isinstance(raw_expire, datetime):
        return date(raw_expire.year, raw_expire.month, 1)
    if isinstance(raw_expire, date):
        return date(raw_expire.year, raw_expire.month, 1)
    if isinstance(raw_expire, (int, float, Decimal)):
        try:
            serial = int(Decimal(str(raw_expire)))
        except InvalidOperation:
            serial = 0
        if serial > 59:
            serial -= 1
        if serial > 0:
            excel_date = date(1899, 12, 31) + timedelta(days=serial)
            return date(excel_date.year, excel_date.month, 1)
    value = stringify_spreadsheet_value(raw_expire)
    if not value:
        raise ValueError("Expire value is required.")
    patterns = [
        r"^(?P<year>\d{4})[-/.](?P<month>\d{1,2})$",
        r"^(?P<month>\d{1,2})[-/.](?P<year>\d{2,4})$",
    ]
    for pattern in patterns:
        match = re.match(pattern, value)
        if not match:
            continue
        month = int(match.group("month"))
        year = int(match.group("year"))
        if year < 100:
            year += 2000
        if 1 <= month <= 12:
            return date(year, month, 1)
    for pattern in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            parsed = datetime.strptime(value, pattern)
            return date(parsed.year, parsed.month, 1)
        except ValueError:
            continue
    raise ValueError("Expire format is invalid.")


def normalize_status(raw_status: Any, default: str | None = None) -> str:
    value = stringify_spreadsheet_value(raw_status).strip().lower()
    if not value and default is not None:
        return default
    aliases = {
        "active": "active",
        "inactive": "inactive",
        "expired": "expired",
        "1": "active",
        "0": "inactive",
    }
    normalized = aliases.get(value)
    if normalized is None:
        raise ValueError("Status must be active, inactive, or expired.")
    return normalized


def prepare_message(card_number: str, balance: Decimal, lang: str = "UZ") -> str:
    amount = Decimal(balance).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    messages = {
        "UZ": f"Sizning kartangiz {card_mask(card_number)} aktiv va foydalanishga {amount} UZS mavjud!",
        "RU": f"Vasha karta {card_mask(card_number)} aktivna, dostupno {amount} UZS!",
        "EN": f"Your card {card_mask(card_number)} is active and has {amount} UZS available!",
    }
    return messages.get(lang.upper(), messages["UZ"])


def send_message(message: str, chat_id: int = 12345) -> dict[str, Any]:
    logger.info("Simulated message sent to chat_id=%s: %s", chat_id, message)
    return {"chat_id": chat_id, "message": message, "sent": True}


def generate_otp(length: int = 6) -> str:
    return "".join(str(random.randint(0, 9)) for _ in range(length))


def send_telegram_message(phone: str, message: str, chat_id: int = 123456) -> dict[str, Any]:
    # Avoid real network calls during tests
    import sys
    is_testing = 'test' in sys.argv or getattr(settings, 'TESTING', False)
    
    token = None if is_testing else getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    
    payload = {
        "phone": format_phone(phone),
        "chat_id": chat_id,
        "message": message,
        "provider": "telegram-simulation",
    }

    if token:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as response:
                payload["sent"] = True
                payload["provider"] = "telegram-api"
        except Exception as e:
            logger.error("Failed to send real telegram message: %s", e)

    logger.info("Telegram send attempt: %s", json.dumps(payload, ensure_ascii=False))
    return payload


def validate_card(card_number: str) -> bool:
    digits = digits_only(card_number)
    if len(digits) != 16:
        return False
    checksum = 0
    reverse_digits = list(map(int, reversed(digits)))
    for index, digit in enumerate(reverse_digits):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def calculate_exchange(amount: Decimal, currency: int) -> Decimal:
    if currency not in EXCHANGE_RATES:
        raise ValueError("Currency is not supported.")
    rate = EXCHANGE_RATES[currency]
    amount = Decimal(amount)
    if currency == 860:
        return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return (amount / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_transfer_by_ext_id(ext_id: str) -> Transfer:
    return Transfer.objects.get(ext_id=ext_id)


def filter_cards_queryset(
    queryset: QuerySet,
    status: str | None = None,
    card_number: str | None = None,
    phone: str | None = None,
) -> QuerySet:
    if status:
        queryset = queryset.filter(status=normalize_status(status))
    if card_number:
        queryset = queryset.filter(card_number=format_card(card_number))
    if phone:
        queryset = queryset.filter(phone=format_phone(phone))
    return queryset


def parse_card_rows(file_name: str, file_bytes: bytes) -> list[dict[str, Any]]:
    suffix = Path(file_name).suffix.lower()
    if suffix == ".csv":
        text = file_bytes.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        return [_normalize_row_keys(row) for row in reader]
    if suffix == ".xlsx":
        return _parse_xlsx_rows(file_bytes)
    raise ValueError("Only .xlsx and .csv files are supported.")


def _parse_xlsx_rows(file_bytes: bytes) -> list[dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
        shared_strings = _read_shared_strings(archive)
        date_style_indexes = _read_date_style_indexes(archive)
        sheet = archive.read("xl/worksheets/sheet1.xml")
    root = ElementTree.fromstring(sheet)
    rows: list[list[Any]] = []
    for row in root.findall(".//main:sheetData/main:row", XLSX_NS):
        values: list[Any] = []
        current_column = 0
        for cell in row.findall("main:c", XLSX_NS):
            ref = cell.attrib.get("r", "")
            column_index = _column_index(ref)
            while current_column < column_index:
                values.append("")
                current_column += 1
            value = _read_cell_value(cell, shared_strings, date_style_indexes)
            values.append(value)
            current_column += 1
        rows.append(values)
    if not rows:
        return []
    headers = [_normalize_header(value) for value in rows[0]]
    return [
        {
            headers[index]: row[index] if index < len(row) else ""
            for index in range(len(headers))
            if headers[index]
        }
        for row in rows[1:]
        if any(stringify_spreadsheet_value(item) for item in row)
    ]


def _normalize_row_keys(row: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        header = _normalize_header(key)
        if header:
            normalized[header] = value
    return normalized


def _normalize_header(value: Any) -> str:
    key = re.sub(r"[^a-z0-9]+", "", stringify_spreadsheet_value(value).strip().lower())
    return HEADER_ALIASES.get(key, "")


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        xml_bytes = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(xml_bytes)
    return ["".join(node.itertext()) for node in root.findall("main:si", XLSX_NS)]


def _read_cell_value(
    cell: ElementTree.Element,
    shared_strings: list[str],
    date_style_indexes: set[int],
) -> Any:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline_node = cell.find("main:is", XLSX_NS)
        if inline_node is None:
            return ""
        return "".join(inline_node.itertext()).strip()
    value_node = cell.find("main:v", XLSX_NS)
    if value_node is None:
        return ""
    value = value_node.text or ""
    if cell_type == "s":
        return shared_strings[int(value)]
    style_index = int(cell.attrib.get("s", "0"))
    if style_index in date_style_indexes:
        try:
            return _excel_serial_to_date(Decimal(value))
        except InvalidOperation:
            return value
    return value


def _read_date_style_indexes(archive: zipfile.ZipFile) -> set[int]:
    try:
        xml_bytes = archive.read("xl/styles.xml")
    except KeyError:
        return set()
    root = ElementTree.fromstring(xml_bytes)
    custom_date_formats: set[int] = set()
    builtin_date_formats = {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}
    for num_fmt in root.findall(".//main:numFmts/main:numFmt", XLSX_NS):
        num_fmt_id = int(num_fmt.attrib.get("numFmtId", "0"))
        format_code = num_fmt.attrib.get("formatCode", "").lower()
        if any(token in format_code for token in ("yy", "mm", "dd", "m/", "d/", "h:", "ss")):
            custom_date_formats.add(num_fmt_id)
    style_indexes: set[int] = set()
    for index, xf in enumerate(root.findall(".//main:cellXfs/main:xf", XLSX_NS)):
        num_fmt_id = int(xf.attrib.get("numFmtId", "0"))
        if num_fmt_id in builtin_date_formats or num_fmt_id in custom_date_formats:
            style_indexes.add(index)
    return style_indexes


def _excel_serial_to_date(value: Decimal) -> date:
    serial = int(value)
    if serial > 59:
        serial -= 1
    return date(1899, 12, 31) + timedelta(days=serial)


def _column_index(reference: str) -> int:
    letters = "".join(ch for ch in reference if ch.isalpha())
    result = 0
    for letter in letters:
        result = result * 26 + (ord(letter.upper()) - ord("A") + 1)
    return max(result - 1, 0)


def normalize_card_row(row: dict[str, Any]) -> dict[str, Any]:
    card_number = format_card(row.get("card_number"))
    if not validate_card(card_number):
        raise ValueError("Card number failed LUHN validation.")
    return {
        "card_number": card_number,
        "expire": parse_expire(row.get("expire")),
        "phone": format_phone(row.get("phone")),
        "status": normalize_status(row.get("status"), default="active"),
        "balance": parse_balance(row.get("balance")),
    }


def build_card_export_rows(queryset, value_style: str = "formatted") -> list[list[str]]:
    rows = [CARD_HEADERS]
    for card in queryset:
        if value_style == "raw":
            rows.append(
                [
                    card.card_number,
                    card.expire.strftime("%Y-%m"),
                    card.phone or "",
                    card.status,
                    format(card.balance, ".2f"),
                ]
            )
            continue
        rows.append(
            [
                display_card(card.card_number),
                card.expire.strftime("%Y-%m"),
                display_phone(card.phone) if card.phone else "",
                card.status.title(),
                format(card.balance, ",.2f"),
            ]
        )
    return rows


def build_cards_xlsx_bytes(queryset, value_style: str = "formatted") -> bytes:
    rows = build_card_export_rows(queryset, value_style=value_style)
    all_values = [stringify_spreadsheet_value(value) for row in rows for value in row]
    shared_strings = [
        """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>""",
        (
            f"""<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" """
            f"""count="{len(all_values)}" uniqueCount="{len(all_values)}">"""
        ),
    ]
    for value in all_values:
        escaped = (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        shared_strings.append(f"<si><t>{escaped}</t></si>")
    shared_strings.append("</sst>")

    sheet_lines = [
        """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>""",
        """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>""",
    ]
    shared_index = 0
    for row_index, row in enumerate(rows, start=1):
        sheet_lines.append(f'<row r="{row_index}">')
        for col_index, _ in enumerate(row):
            cell_ref = f"{chr(65 + col_index)}{row_index}"
            sheet_lines.append(f'<c r="{cell_ref}" t="s"><v>{shared_index}</v></c>')
            shared_index += 1
        sheet_lines.append("</row>")
    sheet_lines.append("</sheetData></worksheet>")

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Cards" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>"""

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/sharedStrings.xml", "".join(shared_strings))
        archive.writestr("xl/worksheets/sheet1.xml", "".join(sheet_lines))
    return output.getvalue()

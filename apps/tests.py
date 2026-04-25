import csv
import json
import os
import tempfile
import zipfile
from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from .models import Card, CardStatus, Error, Transfer, TransferState
from .utils import (
    calculate_exchange,
    display_card,
    format_card,
    format_phone,
    normalize_card_row,
    parse_card_rows,
    parse_expire,
    prepare_message,
    validate_card,
)


class UtilsTests(TestCase):
    def test_card_and_phone_formatting(self):
        self.assertEqual(format_card("8600 0000 0000 0007"), "8600000000000007")
        self.assertEqual(format_phone("99 123 45 67"), "998991234567")
        self.assertEqual(parse_expire("12/26"), date(2026, 12, 1))
        self.assertEqual(parse_expire("April 25, 2029"), date(2029, 4, 1))

    def test_message_and_exchange_helpers(self):
        message = prepare_message("8600000000000007", Decimal("5000.00"))
        self.assertIn("5000.00 UZS", message)
        self.assertEqual(calculate_exchange(Decimal("14500"), 643), Decimal("100.00"))

    def test_luhn_validation(self):
        self.assertTrue(validate_card("8600000000000007"))
        self.assertFalse(validate_card("8600000000000008"))

    def test_parse_xlsx_inline_string_rows(self):
        content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""
        rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
        workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Cards" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
        workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
        sheet = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>card_number</t></is></c>
      <c r="B1" t="inlineStr"><is><t>expire</t></is></c>
      <c r="C1" t="inlineStr"><is><t>phone</t></is></c>
      <c r="D1" t="inlineStr"><is><t>status</t></is></c>
      <c r="E1" t="inlineStr"><is><t>balance</t></is></c>
    </row>
    <row r="2">
      <c r="A2" t="inlineStr"><is><t>8600 0000 0000 0007</t></is></c>
      <c r="B2" t="inlineStr"><is><t>12/26</t></is></c>
      <c r="C2" t="inlineStr"><is><t>99 123 45 67</t></is></c>
      <c r="D2" t="inlineStr"><is><t>active</t></is></c>
      <c r="E2" t="inlineStr"><is><t>15000.00</t></is></c>
    </row>
  </sheetData>
</worksheet>"""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
            with zipfile.ZipFile(tmp.name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("[Content_Types].xml", content_types)
                archive.writestr("_rels/.rels", rels)
                archive.writestr("xl/workbook.xml", workbook)
                archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
                archive.writestr("xl/worksheets/sheet1.xml", sheet)
            with open(tmp.name, "rb") as source:
                rows = parse_card_rows("inline.xlsx", source.read())
        os.unlink(tmp.name)
        self.assertEqual(rows[0]["card_number"], "8600 0000 0000 0007")

    def test_parse_xlsx_inline_string_without_is_node(self):
        content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""
        rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
        workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Cards" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
        workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
        sheet = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>card_number</t></is></c>
      <c r="B1" t="inlineStr"><is><t>expire</t></is></c>
      <c r="C1" t="inlineStr"><is><t>phone</t></is></c>
      <c r="D1" t="inlineStr"><is><t>status</t></is></c>
      <c r="E1" t="inlineStr"><is><t>balance</t></is></c>
    </row>
    <row r="2">
      <c r="A2" t="inlineStr"><is><t>8600 0000 0000 0007</t></is></c>
      <c r="B2" t="inlineStr"></c>
      <c r="C2" t="inlineStr"><is><t>99 123 45 67</t></is></c>
      <c r="D2" t="inlineStr"><is><t>active</t></is></c>
      <c r="E2" t="inlineStr"><is><t>15000.00</t></is></c>
    </row>
  </sheetData>
</worksheet>"""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
            with zipfile.ZipFile(tmp.name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("[Content_Types].xml", content_types)
                archive.writestr("_rels/.rels", rels)
                archive.writestr("xl/workbook.xml", workbook)
                archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
                archive.writestr("xl/worksheets/sheet1.xml", sheet)
            with open(tmp.name, "rb") as source:
                rows = parse_card_rows("inline_missing_node.xlsx", source.read())
        os.unlink(tmp.name)

        self.assertEqual(rows[0]["card_number"], "8600 0000 0000 0007")
        self.assertEqual(rows[0]["expire"], "")

    def test_parse_xlsx_with_excel_dates_and_flexible_headers(self):
        content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""
        rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
        workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Cards" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
        workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>"""
        styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border/></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
  </cellXfs>
</styleSheet>"""
        shared_strings = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="9" uniqueCount="9">
  <si><t>Card Number</t></si>
  <si><t>Expire Date</t></si>
  <si><t>Phone Number</t></si>
  <si><t>Status</t></si>
  <si><t>Balance</t></si>
  <si><t>8600 0000 0000 0007</t></si>
  <si><t>99 123 45 67</t></si>
  <si><t>Active</t></si>
  <si><t>15,000.00</t></si>
</sst>"""
        sheet = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1" t="s"><v>0</v></c>
      <c r="B1" t="s"><v>1</v></c>
      <c r="C1" t="s"><v>2</v></c>
      <c r="D1" t="s"><v>3</v></c>
      <c r="E1" t="s"><v>4</v></c>
    </row>
    <row r="2">
      <c r="A2" t="s"><v>5</v></c>
      <c r="B2" s="1"><v>47840</v></c>
      <c r="C2" t="s"><v>6</v></c>
      <c r="D2" t="s"><v>7</v></c>
      <c r="E2" t="s"><v>8</v></c>
    </row>
  </sheetData>
</worksheet>"""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
            with zipfile.ZipFile(tmp.name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("[Content_Types].xml", content_types)
                archive.writestr("_rels/.rels", rels)
                archive.writestr("xl/workbook.xml", workbook)
                archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
                archive.writestr("xl/styles.xml", styles)
                archive.writestr("xl/sharedStrings.xml", shared_strings)
                archive.writestr("xl/worksheets/sheet1.xml", sheet)
            with open(tmp.name, "rb") as source:
                rows = parse_card_rows("cards.xlsx", source.read())
        os.unlink(tmp.name)

        self.assertEqual(rows[0]["card_number"], "8600 0000 0000 0007")
        self.assertEqual(rows[0]["status"], "Active")


class CommandTests(TestCase):
    def setUp(self):
        self.card = Card.objects.create(
            card_number="8600000000000007",
            expire=date(2026, 12, 1),
            phone="998991234567",
            status=CardStatus.ACTIVE,
            balance=Decimal("150000.00"),
        )

    def test_populate_errors_command(self):
        call_command("populate_errors")
        self.assertTrue(Error.objects.filter(code=32701).exists())

    def test_export_cards_command(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            output = tmp.name
        try:
            call_command("export_cards", "--status=active", f"--output={output}")
            with open(output, newline="", encoding="utf-8") as exported:
                rows = list(csv.reader(exported))
            self.assertEqual(rows[0], ["card_number", "expire", "phone", "status", "balance"])
            self.assertEqual(len(rows), 2)
        finally:
            os.unlink(output)

    def test_export_cards_command_xlsx_raw(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
            output = tmp.name
        try:
            call_command("export_cards", "--status=active", "--value-style=raw", f"--output={output}")
            with open(output, "rb") as exported:
                rows = parse_card_rows("cards.xlsx", exported.read())
            self.assertEqual(rows[0]["card_number"], "8600000000000007")
            self.assertEqual(rows[0]["status"], "active")
        finally:
            os.unlink(output)

    def test_send_fake_messages_command(self):
        out = StringIO()
        call_command("send_fake_messages", "--status=active", stdout=out)
        self.assertIn("Simulated 1 messages.", out.getvalue())

    def test_admin_export_view_filters_by_selected_status(self):
        inactive_card = Card.objects.create(
            card_number="8600000000000015",
            expire=date(2027, 1, 1),
            phone="998901112233",
            status=CardStatus.INACTIVE,
            balance=Decimal("9900.00"),
        )
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="secret123",
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("admin:apps_card_export"),
            data={
                "export_format": "csv",
                "status": CardStatus.ACTIVE,
            },
        )

        rows = list(csv.reader(StringIO(response.content.decode("utf-8"))))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(rows[1][0], display_card(self.card.card_number))
        self.assertNotIn(display_card(inactive_card.card_number), [row[0] for row in rows[1:]])


class CardValidationTests(TestCase):
    def test_card_model_clean_enforces_luhn(self):
        card = Card(
            card_number="8765 4321 2764 9729",
            expire=date(2029, 4, 1),
            phone="+998 93 115 80 60",
            status=CardStatus.ACTIVE,
            balance=Decimal("77080.00"),
        )

        with self.assertRaises(ValidationError) as exc:
            card.full_clean()

        self.assertIn("card_number", exc.exception.message_dict)

    def test_import_row_accepts_spaced_phone_text_status_and_long_date(self):
        row = {
            "card_number": "8600 0000 0000 0007",
            "phone": "+998 93 115 80 60",
            "status": "Active",
            "expire": "April 25, 2029",
            "balance": "77080.00",
        }

        normalized = normalize_card_row(row)

        self.assertEqual(normalized["card_number"], "8600000000000007")
        self.assertEqual(normalized["phone"], "998931158060")
        self.assertEqual(normalized["status"], "active")
        self.assertEqual(normalized["expire"], date(2029, 4, 1))

    def test_import_row_defaults_status_to_active(self):
        row = {
            "card_number": "8600 0000 0000 0007",
            "phone": "+998 93 115 80 60",
            "expire": "April 25, 2029",
            "balance": "77080.00",
        }

        normalized = normalize_card_row(row)

        self.assertEqual(normalized["status"], "active")


class RpcTests(TestCase):
    def setUp(self):
        self.client = Client()
        call_command("populate_errors")
        self.sender = Card.objects.create(
            card_number="8600000000000007",
            expire=date(2026, 12, 1),
            phone="998991234567",
            status=CardStatus.ACTIVE,
            balance=Decimal("250000.00"),
        )
        self.receiver = Card.objects.create(
            card_number="8600000000000015",
            expire=date(2027, 1, 1),
            phone="998997654321",
            status=CardStatus.ACTIVE,
            balance=Decimal("1000.00"),
        )

    def _post(self, payload: dict):
        return self.client.post(
            "/rpc/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_transfer_create_confirm_and_history(self):
        create_response = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "transfer.create",
                "params": {
                    "ext_id": "tr-001",
                    "sender_card_number": self.sender.card_number,
                    "sender_card_expiry": "12/26",
                    "receiver_card_number": self.receiver.card_number,
                    "sending_amount": "15000.00",
                    "currency": 643,
                },
            }
        )
        create_json = create_response.json()
        self.assertTrue(create_json["result"]["otp_sent"])
        transfer = Transfer.objects.get(ext_id="tr-001")

        confirm_response = self._post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "transfer.confirm",
                "params": {"ext_id": "tr-001", "otp": transfer.otp},
            }
        )
        self.assertEqual(confirm_response.json()["result"]["state"], TransferState.CONFIRMED)
        transfer.refresh_from_db()
        self.assertEqual(transfer.state, TransferState.CONFIRMED)

        history_response = self._post(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "transfer.history",
                "params": {"card_number": self.sender.card_number, "status": "confirmed"},
            }
        )
        self.assertEqual(len(history_response.json()["result"]), 1)

    def test_transfer_confirm_wrong_otp(self):
        self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "transfer.create",
                "params": {
                    "ext_id": "tr-002",
                    "sender_card_number": self.sender.card_number,
                    "sender_card_expiry": "12/26",
                    "receiver_card_number": self.receiver.card_number,
                    "sending_amount": "10000.00",
                    "currency": 840,
                },
            }
        )
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "transfer.confirm",
                "params": {"ext_id": "tr-002", "otp": "000000"},
            }
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Attempts left", response.json()["error"]["message"])

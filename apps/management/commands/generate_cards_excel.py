from django.core.management.base import BaseCommand

from apps.utils import CARD_HEADERS

SAMPLE_ROWS = [
    ["8600 4835 2559 2899", "2025-07", "973-03-03", "expired", "200.00"],
    ["8600855254356990", "03.2026", "", "active", "842,714,800.00"],
    ["8600327218840361", "2026-08", "99 973 03 03", "active", "22,300.00"],
    ["8600 3901 0981 2774", "04/25", "", "active", "8,911,200.00"],
    ["8600 0871 2045 1520", "11.2026", "973-03-03", "expired", "400.00"],
    ["8600910092834567", "07/26", "", "expired", "684,214,300.00"],
    ["8600 1234 5678 9012", "12/24", "99 973 03 03", "inactive", "5,000.00"],
    ["8600 7843 9910 1122", "06.2024", "", "inactive", "0.00"],
]


class Command(BaseCommand):
    help = "Generate a sample Excel-compatible .xlsx file with card data."

    def add_arguments(self, parser):
        parser.add_argument("--output", default="sample_cards.xlsx")

    def handle(self, *args, **options):
        output = options["output"]
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
        all_values = CARD_HEADERS + [value for row in SAMPLE_ROWS for value in row]
        shared_strings = [
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>""",
            f"""<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(all_values)}" uniqueCount="{len(all_values)}">""",
        ]
        for value in all_values:
            shared_strings.append(f"<si><t>{value}</t></si>")
        shared_strings.append("</sst>")
        rows = [CARD_HEADERS] + SAMPLE_ROWS
        sheet_lines = [
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>""",
            """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>""",
        ]
        for row_index, row in enumerate(rows, start=1):
            sheet_lines.append(f'<row r="{row_index}">')
            for col_index, _ in enumerate(row):
                cell_ref = f"{chr(65 + col_index)}{row_index}"
                shared_index = sum(len(item) for item in rows[: row_index - 1]) + col_index
                sheet_lines.append(f'<c r="{cell_ref}" t="s"><v>{shared_index}</v></c>')
            sheet_lines.append("</row>")
        sheet_lines.append("</sheetData></worksheet>")

        import zipfile

        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("_rels/.rels", rels)
            archive.writestr("xl/workbook.xml", workbook)
            archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
            archive.writestr("xl/sharedStrings.xml", "".join(shared_strings))
            archive.writestr("xl/worksheets/sheet1.xml", "".join(sheet_lines))
        self.stdout.write(self.style.SUCCESS(f"Sample Excel file created: {output}"))

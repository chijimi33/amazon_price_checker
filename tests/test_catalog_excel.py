import shutil
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries
from openpyxl.worksheet.table import TableColumn

from catalog_excel import build_catalog_from_workbook, convert_value


REPOSITORY_ROOT = Path(__file__).parents[1]
WORKBOOK = REPOSITORY_ROOT / "catalog" / "quality_catalog.xlsx"


def table_geometry(sheet, table_name):
    table = sheet.tables[table_name]
    return table, range_boundaries(table.ref)


def write_first_empty_row(sheet, table_name, values):
    _, (min_col, min_row, max_col, max_row) = table_geometry(sheet, table_name)
    headers = {
        str(sheet.cell(min_row, column).value): column
        for column in range(min_col, max_col + 1)
    }
    for row in range(min_row + 1, max_row + 1):
        if all(sheet.cell(row, column).value in (None, "") for column in range(min_col, max_col + 1)):
            for key, value in values.items():
                sheet.cell(row, headers[key]).value = value
            return row
    raise AssertionError(f"{table_name} has no reserved row")


def fill_minimum_ssd(workbook):
    write_first_empty_row(
        workbook["SSD"],
        "SSDCatalog",
        {
            "product_id": "test-ssd",
            "brand": "Example",
            "model": "EX-2TB",
            "display_name": "Example 2TB SSD",
            "status": "research_required",
            "tier": "unrated",
        },
    )


class CatalogExcelTest(unittest.TestCase):
    def copy_workbook(self):
        temporary = tempfile.TemporaryDirectory()
        destination = Path(temporary.name) / "quality_catalog.xlsx"
        shutil.copy2(WORKBOOK, destination)
        return temporary, destination

    def test_repository_workbook_is_valid_and_matches_json(self):
        import json

        result = build_catalog_from_workbook(WORKBOOK)
        expected = json.loads(
            (REPOSITORY_ROOT / "catalog" / "quality_catalog.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result.errors, [])
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.catalog, expected)

    def test_cpu_catalog_contains_supported_generations_and_performance_data(self):
        result = build_catalog_from_workbook(WORKBOOK)
        self.assertEqual(result.errors, [])
        products = {
            product["id"]: product
            for product in result.catalog["products"]
            if product["category"] == "cpu"
        }
        self.assertGreaterEqual(len(products), 112)

        ryzen = products["amd-ryzen-5-5600x"]
        self.assertEqual(ryzen["specs"]["generation"], "Ryzen 5000")
        self.assertEqual(ryzen["specs"]["release_date"], "2020-11-05")
        self.assertGreater(ryzen["specs"]["passmark_cpu_mark"], 0)
        self.assertGreater(ryzen["specs"]["passmark_single_thread_mark"], 0)

        intel = products["intel-core-i5-12400f"]
        self.assertEqual(intel["specs"]["generation"], "12th Gen Core")
        self.assertEqual(intel["specs"]["release_date"], "2022-01-04")
        self.assertFalse(intel["specs"]["integrated_graphics"])
        self.assertGreaterEqual(len(intel["quality"]["evidence"]), 2)

    def test_new_spec_column_is_mapped_without_converter_change(self):
        temporary, path = self.copy_workbook()
        try:
            workbook = load_workbook(path)
            fill_minimum_ssd(workbook)
            sheet = workbook["SSD"]
            table, (min_col, min_row, max_col, max_row) = table_geometry(sheet, "SSDCatalog")
            new_column = max_col + 1
            sheet.cell(min_row, new_column).value = "test_metric"
            sheet.cell(min_row + 1, new_column).value = 12.5
            table.ref = (
                f"{get_column_letter(min_col)}{min_row}:"
                f"{get_column_letter(new_column)}{max_row}"
            )
            table.tableColumns.append(
                TableColumn(id=len(table.tableColumns) + 1, name="test_metric")
            )
            write_first_empty_row(
                workbook["FieldDefinitions"],
                "FieldDefinitionsCatalog",
                {
                    "sheet_name": "SSD",
                    "column_name": "test_metric",
                    "json_path": "specs.test_metric",
                    "data_type": "number",
                    "required": False,
                    "description": "拡張テスト",
                    "active": True,
                },
            )
            workbook.save(path)

            result = build_catalog_from_workbook(path)
            self.assertEqual(result.errors, [])
            product = next(
                product
                for product in result.catalog["products"]
                if product["id"] == "test-ssd"
            )
            self.assertEqual(product["specs"]["test_metric"], 12.5)
        finally:
            temporary.cleanup()

    def test_related_sheets_are_merged_into_product(self):
        temporary, path = self.copy_workbook()
        try:
            workbook = load_workbook(path)
            fill_minimum_ssd(workbook)
            write_first_empty_row(
                workbook["Identifiers"],
                "IdentifiersCatalog",
                {
                    "product_id": "test-ssd",
                    "identifier_type": "asins",
                    "identifier_value": "B0TEST0001",
                    "region": "JP",
                    "condition": "new",
                    "is_primary": False,
                },
            )
            write_first_empty_row(
                workbook["Evidence"],
                "EvidenceCatalog",
                {
                    "evidence_id": "ev-test-1",
                    "product_id": "test-ssd",
                    "kind": "manufacturer",
                    "title": "Specification",
                    "url": "https://example.com/spec",
                    "checked_at": "2026-07-15",
                    "supports": "specs.capacity_gb\nspecs.nand_type",
                },
            )
            write_first_empty_row(
                workbook["Risks"],
                "RisksCatalog",
                {
                    "risk_id": "risk-test-1",
                    "product_id": "test-ssd",
                    "risk_flag": "component_swap_unverified",
                    "severity": "medium",
                    "status": "monitoring",
                    "summary": "部品変更を継続確認",
                    "evidence_id": "ev-test-1",
                },
            )
            workbook.save(path)

            result = build_catalog_from_workbook(path)
            self.assertEqual(result.errors, [])
            product = next(
                product
                for product in result.catalog["products"]
                if product["id"] == "test-ssd"
            )
            self.assertEqual(product["identifiers"]["asins"], ["B0TEST0001"])
            self.assertEqual(product["quality"]["evidence"][0]["id"], "ev-test-1")
            self.assertEqual(
                product["quality"]["risk_flags"], ["component_swap_unverified"]
            )
            self.assertEqual(product["quality"]["risks"][0]["id"], "risk-test-1")
        finally:
            temporary.cleanup()

    def test_cell_types_are_strict_and_human_friendly(self):
        self.assertEqual(convert_value("A\nB;C", "string_list"), ["A", "B", "C"])
        self.assertTrue(convert_value("TRUE", "boolean"))
        self.assertEqual(convert_value("2.0", "integer"), 2)
        self.assertEqual(convert_value("2026-07-15", "date"), "2026-07-15")
        with self.assertRaises(ValueError):
            convert_value("2.5", "integer")


if __name__ == "__main__":
    unittest.main()

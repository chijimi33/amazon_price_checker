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

    def test_gpu_chip_catalog_contains_supported_series_and_benchmarks(self):
        result = build_catalog_from_workbook(WORKBOOK)
        self.assertEqual(result.errors, [])
        chips = [
            product
            for product in result.catalog["products"]
            if product["category"] == "gpu_chip"
        ]
        self.assertGreaterEqual(len(chips), 64)
        self.assertGreaterEqual(sum(chip["brand"] == "NVIDIA" for chip in chips), 38)
        self.assertGreaterEqual(sum(chip["brand"] == "AMD" for chip in chips), 26)

        products = {product["id"]: product for product in chips}
        rtx = products["nvidia-geforce-rtx-5090"]
        self.assertEqual(rtx["specs"]["generation"], "GeForce RTX 50 Series")
        self.assertEqual(rtx["specs"]["vram_gb"], 32)
        self.assertGreater(rtx["specs"]["passmark_g3d_mark"], 0)

        radeon = products["amd-radeon-rx-9070-gre"]
        self.assertEqual(radeon["specs"]["generation"], "Radeon RX 9000 Series")
        self.assertEqual(radeon["specs"]["release_date"], "2026-06-02")
        self.assertGreaterEqual(len(radeon["quality"]["evidence"]), 2)

        rx6500 = products["amd-radeon-rx-6500-xt-8gb"]
        self.assertEqual(rx6500["specs"]["vram_gb"], 8)
        self.assertGreaterEqual(len(rx6500["quality"]["evidence"]), 2)

        rx6700 = products["amd-radeon-rx-6700"]
        self.assertEqual(rx6700["specs"]["release_date"], "2021-06-09")
        self.assertEqual(rx6700["specs"]["release_date_precision"], "exact")

        profile = next(
            profile
            for profile in result.catalog["profiles"]
            if profile["id"] == "gpu-1440p-16gb"
        )
        cooler_rule = next(
            rule
            for rule in profile["preferences"]
            if rule["path"] == "specs.cooler_quality_tier"
        )
        self.assertIn("S", cooler_rule["value"])

    def test_gpu_board_data_validations_match_their_columns(self):
        workbook = load_workbook(WORKBOOK, data_only=False)
        validations = {
            (str(validation.sqref), validation.formula1)
            for validation in workbook["GPU"].data_validations.dataValidation
        }

        self.assertIn(("W5:X104", '"true,false"'), validations)
        self.assertIn(("AA5:AA104", "'Lists'!$P$5:$P$8"), validations)
        self.assertIn(("AD5:AD104", "'Lists'!$R$5:$R$9"), validations)
        self.assertIn(("AE5:AE104", "'Lists'!$Q$5:$Q$9"), validations)
        self.assertNotIn(("V5:V104", "'Lists'!$P$5:$P$8"), validations)
        self.assertNotIn(("X5:X104", "'Lists'!$R$5:$R$9"), validations)
        self.assertNotIn(("Y5:Y104", "'Lists'!$Q$5:$Q$9"), validations)

    def test_gpu_board_catalog_contains_initial_exact_skus(self):
        result = build_catalog_from_workbook(WORKBOOK)
        self.assertEqual(result.errors, [])
        products = {
            product["id"]: product
            for product in result.catalog["products"]
            if product["category"] == "gpu"
        }
        self.assertGreaterEqual(len(products), 20)
        self.assertTrue(
            {"ASUS", "MSI", "GIGABYTE", "SAPPHIRE", "PowerColor", "ASRock"}
            .issubset({product["brand"] for product in products.values()})
        )

        generations = {
            product["specs"]["gpu_chip"]["generation"]
            for product in products.values()
        }
        self.assertTrue(
            {
                "GeForce RTX 20 Series",
                "GeForce RTX 30 Series",
                "GeForce RTX 40 Series",
                "GeForce RTX 50 Series",
                "Radeon RX 6000 Series",
                "Radeon RX 7000 Series",
                "Radeon RX 9000 Series",
            }.issubset(generations)
        )

        asus = products["gpu-asus-dual-rtx3060-o12g-v2"]
        self.assertEqual(
            asus["identifiers"]["part_numbers"],
            ["DUAL-RTX3060-O12G-V2"],
        )
        self.assertEqual(asus["specs"]["revision"], "V2")
        self.assertEqual(asus["specs"]["length_mm"], 200)

        sapphire = products["gpu-sapphire-11348-03-20g"]
        self.assertEqual(sapphire["specs"]["fan_count"], 3)
        self.assertEqual(sapphire["specs"]["slot_width"], 3)
        self.assertEqual(sapphire["specs"]["power_connector_standard"], "2x8pin")
        self.assertEqual(sapphire["specs"]["tdp_w"], 304)

        msi = products["gpu-msi-g5070-12gtc"]
        self.assertEqual(msi["identifiers"]["part_numbers"], ["G5070-12GTC"])
        self.assertEqual(msi["specs"]["gpu_chip"]["vram_gb"], 12)
        self.assertEqual(msi["specs"]["power_connector_standard"], "12V-2x6")

        for product in products.values():
            self.assertEqual(product["quality"]["status"], "research_required")
            self.assertEqual(product["quality"]["tier"], "unrated")
            self.assertTrue(
                any(
                    evidence["kind"] == "manufacturer"
                    for evidence in product["quality"]["evidence"]
                )
            )

    def test_gpu_board_resolves_normalized_chip_specs(self):
        temporary, path = self.copy_workbook()
        try:
            workbook = load_workbook(path)
            write_first_empty_row(
                workbook["GPUChips"],
                "GPUChipsCatalog",
                {
                    "product_id": "test-gpu-chip",
                    "brand": "NVIDIA",
                    "model": "Test GPU 16GB",
                    "display_name": "NVIDIA Test GPU 16GB",
                    "status": "research_required",
                    "tier": "unrated",
                    "vram_gb": 16,
                    "passmark_g3d_mark": 30000,
                },
            )
            write_first_empty_row(
                workbook["GPU"],
                "GPUCatalog",
                {
                    "product_id": "test-gpu-board",
                    "brand": "ExampleBoard",
                    "model": "EX-GPU-16",
                    "display_name": "ExampleBoard EX-GPU-16",
                    "status": "research_required",
                    "tier": "unrated",
                    "gpu_chip_id": "test-gpu-chip",
                    "board_series": "Example Series",
                    "domestic_warranty_years": 2,
                },
            )
            workbook.save(path)

            result = build_catalog_from_workbook(path)
            self.assertEqual(result.errors, [])
            board = next(
                product
                for product in result.catalog["products"]
                if product["id"] == "test-gpu-board"
            )
            self.assertEqual(board["specs"]["gpu_chip"]["id"], "test-gpu-chip")
            self.assertEqual(board["specs"]["gpu_chip"]["vram_gb"], 16)
            self.assertEqual(board["specs"]["gpu_chip"]["passmark_g3d_mark"], 30000)
        finally:
            temporary.cleanup()

    def test_memory_catalog_contains_initial_exact_sku_kits(self):
        result = build_catalog_from_workbook(WORKBOOK)
        self.assertEqual(result.errors, [])
        products = {
            product["id"]: product
            for product in result.catalog["products"]
            if product["category"] == "memory"
        }
        self.assertGreaterEqual(len(products), 16)
        self.assertEqual(
            {product["brand"] for product in products.values()},
            {"Corsair", "G.SKILL", "Kingston", "Crucial"},
        )

        corsair = products["memory-corsair-cmk32gx5m2b6000z30"]
        self.assertEqual(
            corsair["identifiers"]["part_numbers"],
            ["CMK32GX5M2B6000Z30"],
        )
        self.assertEqual(corsair["specs"]["memory_type"], "DDR5")
        self.assertEqual(corsair["specs"]["total_capacity_gb"], 32)
        self.assertEqual(corsair["specs"]["module_count"], 2)
        self.assertEqual(corsair["specs"]["data_rate_mt_s"], 6000)
        self.assertEqual(corsair["specs"]["default_data_rate_mt_s"], 4800)
        self.assertTrue(corsair["specs"]["expo"])
        self.assertEqual(corsair["specs"]["cas_latency"], 30)

        kingston = products["memory-kingston-kf560c30bbek2-64"]
        self.assertEqual(kingston["specs"]["module_height_mm"], 34.9)
        self.assertEqual(kingston["specs"]["warranty_class"], "limited_lifetime")

        crucial = products["memory-crucial-cp2k16g4dfra32a"]
        self.assertEqual(crucial["specs"]["memory_type"], "DDR4")
        self.assertFalse(crucial["specs"]["expo"])
        self.assertEqual(crucial["specs"]["voltage_v"], 1.2)

        initial_ids = {
            "memory-corsair-cmk32gx5m2b6000z30",
            "memory-corsair-cmk64gx5m2b6000z30",
            "memory-corsair-cmk32gx4m2e3200c16",
            "memory-corsair-cmk64gx4m2e3200c16",
            "memory-gskill-f5-6000j3038f16gx2-fx5",
            "memory-gskill-f5-6000j3040g32gx2-fx5",
            "memory-gskill-f4-3600c16d-32gvkc",
            "memory-gskill-f4-3600c18d-64gvk",
            "memory-kingston-kf560c30bbek2-32",
            "memory-kingston-kf560c30bbek2-64",
            "memory-kingston-kf432c16bbk2-32",
            "memory-kingston-kf432c16bbk2-64",
            "memory-crucial-cp2k16g60c36u5b",
            "memory-crucial-cp2k32g60c40u5b",
            "memory-crucial-cp2k16g4dfra32a",
            "memory-crucial-cp2k32g4dfra32a",
        }
        expected_default_data_rates = {
            "memory-corsair-cmk32gx5m2b6000z30": 4800,
            "memory-corsair-cmk64gx5m2b6000z30": 4800,
            "memory-corsair-cmk32gx4m2e3200c16": 2133,
            "memory-corsair-cmk64gx4m2e3200c16": 2133,
            "memory-gskill-f5-6000j3038f16gx2-fx5": 4800,
            "memory-gskill-f5-6000j3040g32gx2-fx5": 4800,
            "memory-gskill-f4-3600c16d-32gvkc": 2133,
            "memory-gskill-f4-3600c18d-64gvk": 2666,
            "memory-kingston-kf560c30bbek2-32": 4800,
            "memory-kingston-kf560c30bbek2-64": 4800,
            "memory-kingston-kf432c16bbk2-32": 2400,
            "memory-kingston-kf432c16bbk2-64": 2400,
            "memory-crucial-cp2k16g60c36u5b": 5600,
            "memory-crucial-cp2k32g60c40u5b": 5600,
            "memory-crucial-cp2k16g4dfra32a": 3200,
            "memory-crucial-cp2k32g4dfra32a": 3200,
        }
        self.assertTrue(initial_ids.issubset(products))
        for product_id in initial_ids:
            product = products[product_id]
            self.assertEqual(
                product["specs"]["default_data_rate_mt_s"],
                expected_default_data_rates[product_id],
            )
            self.assertEqual(product["quality"]["status"], "research_required")
            self.assertEqual(product["quality"]["tier"], "unrated")
            self.assertGreaterEqual(len(product["quality"]["evidence"]), 1)
            self.assertTrue(
                any(
                    item["kind"] == "manufacturer"
                    for item in product["quality"]["evidence"]
                )
            )

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

    def test_duplicate_json_path_is_rejected_before_silent_overwrite(self):
        temporary, path = self.copy_workbook()
        try:
            workbook = load_workbook(path)
            sheet = workbook["FieldDefinitions"]
            _, (min_col, min_row, max_col, max_row) = table_geometry(
                sheet,
                "FieldDefinitionsCatalog",
            )
            headers = {
                str(sheet.cell(min_row, column).value): column
                for column in range(min_col, max_col + 1)
            }
            cpu_rows = [
                row
                for row in range(min_row + 1, max_row + 1)
                if sheet.cell(row, headers["sheet_name"]).value == "CPU"
            ]
            first, second = cpu_rows[:2]
            duplicate_path = sheet.cell(first, headers["json_path"]).value
            sheet.cell(second, headers["json_path"]).value = duplicate_path
            workbook.save(path)

            result = build_catalog_from_workbook(path)
            self.assertTrue(
                any("JSONパスが重複" in error for error in result.errors)
            )
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

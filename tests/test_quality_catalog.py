import copy
import datetime as dt
import json
import unittest
from pathlib import Path

from quality_catalog import (
    attach_catalog_evaluation,
    evaluate_product,
    find_products,
    match_products,
    profile_by_id,
    validate_catalog,
)


def sample_catalog():
    return {
        "schema_version": 1,
        "updated_at": "2026-07-15",
        "settings": {
            "quality_tier_order": ["unrated", "C", "B", "A", "S"],
            "default_quality_gate": {
                "allowed_statuses": ["approved", "preferred"],
                "minimum_tier": "B",
                "minimum_evidence_count": 2,
                "max_review_age_days": 730,
                "forbidden_risk_flags": ["component_swap_unverified"],
            },
        },
        "profiles": [
            {
                "id": "ssd-2tb",
                "name": "2TB SSD",
                "category": "ssd",
                "requirements": [
                    {
                        "path": "specs.capacity_gb",
                        "op": "gte",
                        "value": 1900,
                        "label": "2TB class",
                    }
                ],
                "preferences": [
                    {
                        "path": "specs.nand_type",
                        "op": "eq",
                        "value": "TLC",
                        "weight": 60,
                        "label": "TLC",
                    },
                    {
                        "path": "specs.dram_cache",
                        "op": "eq",
                        "value": True,
                        "weight": 40,
                        "label": "DRAM",
                    },
                ],
            }
        ],
        "products": [
            {
                "id": "good-ssd",
                "category": "ssd",
                "brand": "Good",
                "model": "FAST-2TB",
                "aliases": ["FAST2T"],
                "identifiers": {
                    "asins": ["B0GOOD0001"],
                    "part_numbers": ["FAST-2TB"],
                },
                "specs": {
                    "capacity_gb": 2000,
                    "nand_type": "TLC",
                    "dram_cache": True,
                },
                "quality": {
                    "status": "approved",
                    "tier": "A",
                    "summary": "tested",
                    "strengths": [],
                    "weaknesses": [],
                    "risk_flags": [],
                    "reviewed_at": "2026-01-01",
                    "evidence": [
                        {
                            "kind": "manufacturer",
                            "title": "Specification",
                            "url": "https://example.com/spec",
                            "checked_at": "2026-01-01",
                        },
                        {
                            "kind": "independent_review",
                            "title": "Review",
                            "url": "https://example.com/review",
                            "checked_at": "2026-01-01",
                        },
                    ],
                },
            },
            {
                "id": "risky-ssd",
                "category": "ssd",
                "brand": "Risky",
                "model": "SWAP-2TB",
                "aliases": [],
                "identifiers": {
                    "asins": ["B0RISK0001"],
                    "part_numbers": ["SWAP-2TB"],
                },
                "specs": {
                    "capacity_gb": 2000,
                    "nand_type": "TLC",
                    "dram_cache": True,
                },
                "quality": {
                    "status": "approved",
                    "tier": "A",
                    "summary": "parts unknown",
                    "strengths": [],
                    "weaknesses": [],
                    "risk_flags": ["component_swap_unverified"],
                    "reviewed_at": "2026-01-01",
                    "evidence": [
                        {
                            "kind": "manufacturer",
                            "title": "Specification",
                            "url": "https://example.com/spec2",
                            "checked_at": "2026-01-01",
                        },
                        {
                            "kind": "independent_review",
                            "title": "Review",
                            "url": "https://example.com/review2",
                            "checked_at": "2026-01-01",
                        },
                    ],
                },
            },
        ],
    }


class QualityCatalogTest(unittest.TestCase):
    def test_catalog_validation_and_repository_catalog(self):
        errors, warnings = validate_catalog(sample_catalog())
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

        repository_catalog = Path(__file__).parents[1] / "catalog" / "quality_catalog.json"
        errors, _ = validate_catalog(json.loads(repository_catalog.read_text(encoding="utf-8")))
        self.assertEqual(errors, [])

    def test_runtime_validation_rejects_motherboard_front_usb_aggregate_mismatch(self):
        repository_catalog = Path(__file__).parents[1] / "catalog" / "quality_catalog.json"
        catalog = json.loads(repository_catalog.read_text(encoding="utf-8"))
        motherboard = next(
            product for product in catalog["products"] if product["category"] == "motherboard"
        )
        motherboard["specs"]["front_usb_c_max_speed_gbps"] += 5

        errors, _ = validate_catalog(catalog)
        self.assertTrue(
            any("front_usb_c_max_speed_gbps" in error and "usb_ports集計値" in error for error in errors)
        )

    def test_runtime_validation_rejects_motherboard_m2_aggregate_mismatches(self):
        repository_catalog = Path(__file__).parents[1] / "catalog" / "quality_catalog.json"
        catalog = json.loads(repository_catalog.read_text(encoding="utf-8"))
        motherboard = next(
            product for product in catalog["products"] if product["category"] == "motherboard"
        )
        for key in ("m2_slots", "pcie5_m2_slots", "m2_heatsink_slots"):
            motherboard["specs"][key] += 1

        errors, _ = validate_catalog(catalog)
        for key in ("m2_slots", "pcie5_m2_slots", "m2_heatsink_slots"):
            with self.subTest(key=key):
                self.assertTrue(
                    any(key in error and "slots集計値" in error for error in errors)
                )

    def test_profile_evaluation_is_explainable(self):
        catalog = sample_catalog()
        profile = profile_by_id(catalog, "ssd-2tb")
        evaluation = evaluate_product(
            catalog,
            catalog["products"][0],
            profile,
            as_of=dt.date(2026, 7, 15),
        )
        self.assertTrue(evaluation["automation_eligible"])
        self.assertEqual(evaluation["preference_score"], 100.0)
        self.assertTrue(all(rule["matched"] for rule in evaluation["requirements"]))

    def test_risk_flag_blocks_automation_but_remains_discoverable(self):
        catalog = sample_catalog()
        profile = profile_by_id(catalog, "ssd-2tb")
        approved = find_products(
            catalog,
            profile=profile,
            as_of=dt.date(2026, 7, 15),
        )
        self.assertEqual([row["product"]["id"] for row in approved], ["good-ssd"])

        all_matches = find_products(
            catalog,
            profile=profile,
            include_ineligible=True,
            as_of=dt.date(2026, 7, 15),
        )
        risky = next(row for row in all_matches if row["product"]["id"] == "risky-ssd")
        self.assertFalse(risky["evaluation"]["automation_eligible"])
        self.assertIn(
            "禁止リスク: component_swap_unverified",
            risky["evaluation"]["quality_gate"]["failures"],
        )

    def test_match_prefers_exact_identity_and_can_attach_to_offer(self):
        catalog = sample_catalog()
        matches = match_products(catalog, asin="b0good0001", title="Good FAST-2TB NVMe")
        self.assertEqual(matches[0]["product"]["id"], "good-ssd")
        self.assertEqual(matches[0]["confidence"], 100)

        result = attach_catalog_evaluation(
            catalog,
            {
                "asin": "B0GOOD0001",
                "product_name": "Good FAST-2TB NVMe",
                "part_number": "FAST-2TB",
            },
            profile_ids=["ssd-2tb"],
            as_of=dt.date(2026, 7, 15),
        )
        self.assertEqual(result["match_status"], "matched")
        self.assertTrue(result["automation_eligible"])

    def test_unknown_offer_is_not_automatically_approved(self):
        result = attach_catalog_evaluation(
            sample_catalog(),
            {"asin": "B0UNKNOWN1", "product_name": "Unknown SSD"},
        )
        self.assertEqual(result["match_status"], "not_cataloged")
        self.assertFalse(result["automation_eligible"])

    def test_runtime_validation_rejects_schema_shape_errors(self):
        catalog = copy.deepcopy(sample_catalog())
        catalog["updated_at"] = "not-a-date"
        catalog["settings"]["quality_tier_order"].append("A")
        catalog["settings"]["default_quality_gate"]["minimum_evidence_count"] = "2"
        catalog["profiles"][0]["requirements"][0] = {
            "path": "specs.capacity_gb",
            "op": "exists",
            "value": "true",
        }
        catalog["profiles"][0]["preferences"] = "not-a-list"
        catalog["products"][0]["aliases"] = "FAST2T"
        catalog["products"][0]["quality"]["evidence"][0]["url"] = "not-a-url"
        catalog["products"][1]["identifiers"] = []

        errors, _ = validate_catalog(catalog)
        self.assertTrue(any("updated_at" in error for error in errors))
        self.assertTrue(any("quality_tier_order に重複" in error for error in errors))
        self.assertTrue(any("minimum_evidence_count" in error for error in errors))
        self.assertTrue(any("真偽値" in error for error in errors))
        self.assertTrue(any("preferences は配列" in error for error in errors))
        self.assertTrue(any("aliases" in error for error in errors))
        self.assertTrue(any("完全なURL" in error for error in errors))
        self.assertTrue(any("identifiers はオブジェクト" in error for error in errors))

    def test_runtime_validation_rejects_missing_evidence_support_path(self):
        catalog = copy.deepcopy(sample_catalog())
        catalog["products"][0]["quality"]["evidence"][0]["supports"] = [
            "specs.release_date"
        ]

        errors, _ = validate_catalog(catalog)
        self.assertTrue(
            any(
                "存在しない製品項目" in error and "specs.release_date" in error
                for error in errors
            )
        )


if __name__ == "__main__":
    unittest.main()

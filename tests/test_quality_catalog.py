import datetime as dt
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
        import json

        errors, _ = validate_catalog(json.loads(repository_catalog.read_text(encoding="utf-8")))
        self.assertEqual(errors, [])

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


if __name__ == "__main__":
    unittest.main()

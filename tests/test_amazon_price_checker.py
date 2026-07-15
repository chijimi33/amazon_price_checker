import json
import tempfile
import unittest
from pathlib import Path

from amazon_price_checker import (
    add_year_low,
    append_history,
    attach_quality_catalog_to_result,
    build_chimolog_result,
    build_confirmation_result,
    build_result,
    extract_asin,
    load_config,
    normalize_confirmations,
    select_primary_offer,
)
from test_quality_catalog import sample_catalog


def sample_item(price=20000, points=200, availability="IN_STOCK"):
    return {
        "asin": "B0TEST0001",
        "detailPageURL": "https://www.amazon.co.jp/dp/B0TEST0001?tag=sample-22",
        "itemInfo": {
            "title": {"displayValue": "Sample Ryzen CPU"},
            "byLineInfo": {"brand": {"displayValue": "AMD"}},
            "manufactureInfo": {
                "model": {"displayValue": "100-TEST"},
                "itemPartNumber": {"displayValue": "100-TEST-BOX"},
                "warranty": {"displayValue": "3 years"},
            },
        },
        "offersV2": {
            "listings": [
                {
                    "availability": {"type": availability, "message": "在庫あり"},
                    "condition": {"value": "New", "subCondition": "Unknown"},
                    "dealDetails": {
                        "accessType": "PRIME_EXCLUSIVE",
                        "badge": "Prime限定セール",
                        "endTime": "2026-07-15T14:59:59Z",
                    },
                    "isBuyBoxWinner": True,
                    "loyaltyPoints": {"points": points},
                    "merchantInfo": {"name": "Amazon.co.jp", "id": "AN1VRQENFRJN5"},
                    "price": {
                        "money": {"amount": price, "currency": "JPY", "displayAmount": f"￥{price:,}"},
                        "savingBasis": {"money": {"amount": 25000, "currency": "JPY"}},
                        "savings": {"money": {"amount": 5000}, "percentage": 20},
                    },
                    "type": "LIGHTNING_DEAL",
                }
            ]
        },
    }


def complete_confirmation(**overrides):
    value = {
        "asin": "B0TEST0001",
        "confirmed_at": "2026-07-14T10:00:00+09:00",
        "price_yen": 19000,
        "points_yen": 500,
        "coupon_checked": True,
        "coupon_discount_yen": 1000,
        "coupon_applied": True,
        "register_discount_checked": True,
        "register_discount_yen": 0,
        "register_discount_applied": False,
        "shipping_yen": 0,
        "seller": "Amazon.co.jp",
        "seller_trusted": True,
        "shipper": "Amazon.co.jp",
        "availability": "在庫あり",
        "cartable": True,
        "condition": "新品",
        "condition_matches": True,
        "model": "100-TEST",
        "model_matches": True,
        "warranty": "3 years",
        "prime_exclusive": True,
    }
    value.update(overrides)
    return value


class AmazonPriceCheckerTest(unittest.TestCase):
    def test_extract_asin_from_supported_inputs(self):
        self.assertEqual(extract_asin("b0test0001"), "B0TEST0001")
        self.assertEqual(
            extract_asin("https://www.amazon.co.jp/gp/product/B0TEST0001?th=1"),
            "B0TEST0001",
        )

    def test_offer_selection_prefers_buy_box_then_lower_available_price(self):
        offers = [
            {"price_yen": 22000, "availability": {"type": "IN_STOCK"}},
            {"price_yen": 21000, "availability": {"type": "IN_STOCK"}},
        ]
        self.assertEqual(select_primary_offer(offers)["price_yen"], 21000)
        offers[0]["is_buy_box_winner"] = True
        self.assertEqual(select_primary_offer(offers)["price_yen"], 22000)

    def test_page_confirmation_wins_and_effective_price_is_separate(self):
        raw = {"itemsResult": {"items": [sample_item()]}, "errors": []}
        confirmations = normalize_confirmations({"items": [complete_confirmation()]})

        result = build_result(
            raw,
            requested_asins=["B0TEST0001"],
            search_count=0,
            marketplace="www.amazon.co.jp",
            confirmations=confirmations,
            history=[],
            fetched_at="2026-07-14T09:59:00+09:00",
        )

        product = result["products"][0]
        self.assertEqual(result["source_status"], "ok")
        self.assertEqual(product["payment"]["base_price_yen"], 19000)
        self.assertEqual(product["payment"]["payment_amount_yen"], 18000)
        self.assertEqual(product["payment"]["points_yen"], 500)
        self.assertEqual(product["payment"]["effective_price_yen"], 17500)
        self.assertEqual(
            product["source_comparison"]["price_difference_page_minus_api_yen"],
            -1000,
        )
        self.assertEqual(product["verification"]["status"], "product_page_confirmed")
        self.assertEqual(product["shipper"], "Amazon.co.jp")

    def test_confirmed_page_does_not_assume_free_shipping(self):
        confirmation = complete_confirmation()
        confirmation.pop("shipping_yen")
        result = build_result(
            {"itemsResult": {"items": [sample_item()]}, "errors": []},
            requested_asins=["B0TEST0001"],
            search_count=0,
            marketplace="www.amazon.co.jp",
            confirmations=normalize_confirmations({"items": [confirmation]}),
            history=[],
            fetched_at="2026-07-14T10:00:00+09:00",
        )
        payment = result["products"][0]["payment"]
        self.assertIsNone(payment["shipping_yen"])
        self.assertIsNone(payment["payment_amount_yen"])
        self.assertIsNone(payment["effective_price_yen"])
        self.assertIn(
            "shipping_yen",
            result["products"][0]["verification"]["missing_product_page_fields"],
        )

    def test_unconfirmed_product_is_explicit_verification_candidate(self):
        result = build_result(
            {"itemsResult": {"items": [sample_item()]}, "errors": []},
            requested_asins=["B0TEST0001"],
            search_count=0,
            marketplace="www.amazon.co.jp",
            confirmations={},
            history=[],
            fetched_at="2026-07-14T10:00:00+09:00",
        )
        product = result["products"][0]
        self.assertEqual(product["verification"]["status"], "amazon_verification_candidate")
        self.assertIn("shipper", product["verification"]["missing_product_page_fields"])
        self.assertTrue(product["verification"]["purchase_before_check"])
        self.assertIsNone(product["shipper"])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            self.assertEqual(append_history(path, result["products"]), 0)
            self.assertFalse(path.exists())

    def test_page_price_increase_and_uncartable_are_excluded(self):
        confirmation = complete_confirmation(price_yen=21000, cartable=False)
        result = build_result(
            {"itemsResult": {"items": [sample_item()]}, "errors": []},
            requested_asins=["B0TEST0001"],
            search_count=0,
            marketplace="www.amazon.co.jp",
            confirmations=normalize_confirmations({"items": [confirmation]}),
            history=[],
            fetched_at="2026-07-14T10:00:00+09:00",
        )
        reasons = result["products"][0]["verification"]["exclusion_reasons"]
        self.assertIn("公式商品ページでカート投入不可", reasons)
        self.assertIn("公式商品ページでAPI取得価格より値上がり", reasons)
        self.assertEqual(result["products"][0]["verification"]["status"], "excluded")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            self.assertEqual(append_history(path, result["products"]), 0)
            self.assertFalse(path.exists())

    def test_unreviewed_third_party_seller_remains_verification_candidate(self):
        confirmation = complete_confirmation(
            seller="Example PC Parts",
            seller_trusted=None,
        )
        result = build_result(
            {"itemsResult": {"items": [sample_item()]}, "errors": []},
            requested_asins=["B0TEST0001"],
            search_count=0,
            marketplace="www.amazon.co.jp",
            confirmations=normalize_confirmations({"items": [confirmation]}),
            history=[],
            fetched_at="2026-07-14T10:00:00+09:00",
        )
        product = result["products"][0]
        self.assertIsNone(product["seller_trusted"])
        self.assertEqual(
            product["verification"]["status"],
            "amazon_verification_candidate",
        )
        self.assertIn(
            "seller_trusted",
            product["verification"]["missing_product_page_fields"],
        )

    def test_missing_requested_asin_is_a_non_silent_error(self):
        result = build_result(
            {"itemsResult": {"items": []}, "errors": []},
            requested_asins=["B0TEST0001"],
            search_count=0,
            marketplace="www.amazon.co.jp",
            confirmations={},
            history=[],
            fetched_at="2026-07-14T10:00:00+09:00",
        )
        self.assertEqual(result["source_status"], "error")
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["products"][0]["record_status"], "error")

    def test_partial_api_failure_requests_monitor_error(self):
        result = build_result(
            {"itemsResult": {"items": [sample_item()]}, "errors": []},
            requested_asins=["B0TEST0001", "B0MISSING01"],
            search_count=0,
            marketplace="www.amazon.co.jp",
            confirmations={},
            history=[],
            fetched_at="2026-07-14T10:00:00+09:00",
        )
        self.assertEqual(result["source_status"], "partial")
        self.assertTrue(result["required_source_failure"])
        self.assertEqual(result["monitor_status_hint"], "監視エラー")
        self.assertEqual(result["acquired_count"], 1)
        self.assertEqual(result["error_count"], 1)

    def test_history_is_scoped_to_year_asin_and_condition(self):
        result = build_result(
            {"itemsResult": {"items": [sample_item()]}, "errors": []},
            requested_asins=["B0TEST0001"],
            search_count=0,
            marketplace="www.amazon.co.jp",
            confirmations=normalize_confirmations({"items": [complete_confirmation()]}),
            history=[
                {
                    "year": 2026,
                    "asin": "B0TEST0001",
                    "condition": "New",
                    "effective_price_yen": 20500,
                },
                {
                    "year": 2025,
                    "asin": "B0TEST0001",
                    "condition": "New",
                    "effective_price_yen": 10000,
                },
            ],
            fetched_at="2026-07-14T10:00:00+09:00",
        )
        reference = result["products"][0]["year_low_reference"]
        self.assertEqual(reference["lowest_effective_price_yen_before"], 20500)
        self.assertTrue(reference["is_new_year_low"])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            self.assertEqual(append_history(path, result["products"]), 1)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["effective_price_yen"], 17500)
            self.assertEqual(stored["condition_key"], "new")

    def test_load_config_accepts_discovery_searches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "asins": ["https://www.amazon.co.jp/dp/B0TEST0001"],
                        "searches": [{"id": "ssd", "keywords": "NVMe SSD"}],
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(path, [])
            self.assertEqual(config["asins"], ["B0TEST0001"])
            self.assertEqual(config["searches"][0]["id"], "ssd")

    def test_quality_catalog_is_attached_without_changing_price_facts(self):
        raw = {"itemsResult": {"items": [sample_item()]}, "errors": []}
        result = build_result(
            raw,
            requested_asins=["B0TEST0001"],
            search_count=0,
            marketplace="www.amazon.co.jp",
            confirmations=normalize_confirmations({"items": [complete_confirmation()]}),
            history=[],
            fetched_at="2026-07-14T10:00:00+09:00",
        )
        catalog = sample_catalog()
        catalog["products"][0]["identifiers"]["asins"] = ["B0TEST0001"]
        catalog["products"][0]["identifiers"]["part_numbers"] = ["100-TEST-BOX"]
        attach_quality_catalog_to_result(
            result,
            catalog,
            profile_ids=["ssd-2tb"],
        )
        product = result["products"][0]
        self.assertEqual(product["payment"]["effective_price_yen"], 17500)
        self.assertEqual(product["quality_catalog"]["catalog_product_id"], "good-ssd")
        self.assertTrue(product["quality_catalog"]["automation_eligible"])

    def test_chimolog_price_is_reference_only_until_product_page_confirmation(self):
        raw = {
            "source": "test chimolog sources",
            "source_status": "ok",
            "required_source_failure": False,
            "fetched_at": "2026-07-15T01:00:00+09:00",
            "source_calls": [],
            "errors": [],
            "metadata": {"selected_item_count": 1},
            "items": [
                {
                    "asin": "B0TEST0001",
                    "title": "Sample Ryzen CPU",
                    "category": "PCパーツ",
                    "themes": ["best"],
                    "theme_labels": ["特におすすめ★5"],
                    "price_yen": 18000,
                    "points_total": 1000,
                    "fetched_at": "2026/07/15 00:05",
                    "is_discounted": True,
                    "adopted_public_source": "chimolog_direct_json",
                    "source_comparison": {"has_difference": False},
                }
            ],
        }
        result = build_chimolog_result(
            raw,
            requested_asins=[],
            marketplace="www.amazon.co.jp",
            confirmations={},
            history=[],
        )
        product = result["products"][0]
        self.assertTrue(product["payment"]["reference_only"])
        self.assertEqual(product["payment"]["effective_price_yen"], 17000)
        self.assertEqual(
            product["source_comparison"]["adopted_source"],
            "chimolog_public_json_reference",
        )
        self.assertNotIn("creators_api", product["source_comparison"])
        self.assertEqual(product["verification"]["status"], "amazon_verification_candidate")

        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                append_history(Path(directory) / "history.jsonl", result["products"]),
                0,
            )

    def test_chimolog_candidate_uses_confirmed_amazon_page_price(self):
        raw = {
            "source_status": "partial",
            "required_source_failure": True,
            "monitor_status_hint": "監視エラー",
            "errors": [{"stage": "required_source", "message": "failed"}],
            "items": [
                {
                    "asin": "B0TEST0001",
                    "title": "Sample Ryzen CPU",
                    "price_yen": 20000,
                    "points_total": 200,
                    "fetched_at": "2026/07/15 00:05",
                }
            ],
        }
        result = build_chimolog_result(
            raw,
            requested_asins=["B0TEST0001"],
            marketplace="www.amazon.co.jp",
            confirmations=normalize_confirmations({"items": [complete_confirmation()]}),
            history=[],
        )
        product = result["products"][0]
        self.assertFalse(product["payment"]["reference_only"])
        self.assertEqual(product["payment"]["payment_amount_yen"], 18000)
        self.assertEqual(
            product["source_comparison"]["adopted_source"],
            "amazon_product_page",
        )
        self.assertNotIn("creators_api", product["source_comparison"])
        self.assertTrue(result["required_source_failure"])
        self.assertEqual(result["monitor_status_hint"], "監視エラー")

    def test_confirmation_only_mode_needs_no_api_or_chimolog(self):
        confirmation = complete_confirmation(
            title="Sample Ryzen CPU",
            acquisition_status="ok",
        )
        result = build_confirmation_result(
            {"source_status": "ok", "errors": []},
            requested_asins=["B0TEST0001"],
            marketplace="www.amazon.co.jp",
            confirmations=normalize_confirmations({"items": [confirmation]}),
            history=[],
        )
        product = result["products"][0]
        self.assertEqual(result["source_status"], "ok")
        self.assertFalse(result["required_source_failure"])
        self.assertEqual(result["source"], "Amazon.co.jp official product-page confirmations (API-free)")
        self.assertEqual(product["product_name"], "Sample Ryzen CPU")
        self.assertEqual(product["payment"]["effective_price_yen"], 17500)
        self.assertEqual(
            product["source_comparison"]["adopted_source"],
            "amazon_product_page",
        )

    def test_confirmation_only_failure_remains_nonempty(self):
        result = build_confirmation_result(
            {
                "source_status": "error",
                "required_source_failure": True,
                "errors": [
                    {
                        "asin": "B0TEST0001",
                        "stage": "amazon_product_page",
                        "message": "CAPTCHA",
                    }
                ],
            },
            requested_asins=["B0TEST0001"],
            marketplace="www.amazon.co.jp",
            confirmations=normalize_confirmations(
                {
                    "items": [
                        {
                            "asin": "B0TEST0001",
                            "acquisition_status": "captcha",
                            "error": "CAPTCHA",
                        }
                    ]
                }
            ),
            history=[],
        )
        self.assertEqual(result["source_status"], "error")
        self.assertEqual(result["monitor_status_hint"], "監視エラー")
        self.assertTrue(result["required_source_failure"])
        self.assertEqual(result["products"][0]["record_status"], "error")
        self.assertEqual(
            result["products"][0]["verification"]["status"],
            "acquisition_failed",
        )


if __name__ == "__main__":
    unittest.main()

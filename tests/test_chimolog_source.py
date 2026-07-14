import json
import tempfile
import unittest
from pathlib import Path

from chimolog_source import fetch_chimolog


def direct_data():
    return {
        "meta": {
            "total": 1,
            "discount_count": 1,
            "updated_at": "2026/07/15 00:06",
            "show_points": True,
        },
        "products": [
            {
                "asin": "B0TEST0001",
                "title": "Fast NVMe SSD 2TB",
                "affiliate_url": "https://amzn.to/example",
                "review_url": "",
                "category": "SSD",
                "themes": ["best"],
                "image_url": "https://example.com/image.jpg",
                "price": 9000,
                "discount": {"ref_high": 12000, "rate_percent": 25},
                "points": {"total": 900, "rate_percent": 10},
                "fetched_at": "2026/07/15 00:05",
            }
        ],
    }


def github_data():
    return {
        "source_url": "https://chimolog.co/wp-content/price/",
        "data_url": "https://chimolog.co/wp-content/price/data/products.json",
        "fetched_at": "2026-07-14T12:10:00+09:00",
        "site": {"updated_at": "2026/07/14 12:06"},
        "count": 1,
        "discount_count": 1,
        "items": [
            {
                "asin": "B0TEST0001",
                "title": "Fast NVMe SSD 2TB",
                "category": "SSD",
                "themes": ["best"],
                "theme_labels": ["特におすすめ★5"],
                "price_yen": 10000,
                "reference_high_yen": 12000,
                "discount_amount_yen": 2000,
                "discount_rate_percent": 16.7,
                "is_discounted": True,
                "points_total": 500,
                "points_rate_percent": 5,
                "fetched_at": "2026/07/14 12:05",
                "product_url": "https://amzn.to/example",
                "affiliate_url": "https://amzn.to/example",
                "review_url": None,
                "image_url": "https://example.com/image.jpg",
            }
        ],
    }


class ChimologSourceTest(unittest.TestCase):
    def write_sources(self, directory):
        root = Path(directory)
        page = root / "price.html"
        direct = root / "direct.json"
        github = root / "github.json"
        page.write_text("<title>Amazonセールおすすめ商品一覧</title>", encoding="utf-8")
        direct.write_text(json.dumps(direct_data()), encoding="utf-8")
        github.write_text(json.dumps(github_data()), encoding="utf-8")
        return page, direct, github

    def test_required_sources_are_compared_and_direct_data_is_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            page, direct, github = self.write_sources(directory)
            result = fetch_chimolog(
                page_file=page,
                direct_file=direct,
                github_file=github,
                categories=["SSD"],
                discounted_only=True,
            )

        self.assertEqual(result["source_status"], "ok")
        self.assertFalse(result["required_source_failure"])
        self.assertEqual(result["metadata"]["selected_item_count"], 1)
        item = result["items"][0]
        self.assertEqual(item["adopted_public_source"], "chimolog_direct_json")
        self.assertEqual(item["price_yen"], 9000)
        self.assertEqual(item["effective_price_reference_yen"], 8100)
        comparison = item["source_comparison"]
        self.assertTrue(comparison["has_difference"])
        self.assertEqual(comparison["price_difference_direct_minus_github_yen"], -1000)
        self.assertEqual(comparison["points_difference_direct_minus_github"], 400)

    def test_required_source_failure_is_non_silent_with_partial_data(self):
        with tempfile.TemporaryDirectory() as directory:
            page, direct, github = self.write_sources(directory)
            github.unlink()
            result = fetch_chimolog(
                page_file=page,
                direct_file=direct,
                github_file=github,
                strict_sources=True,
            )

        self.assertEqual(result["source_status"], "partial")
        self.assertTrue(result["required_source_failure"])
        self.assertEqual(result["monitor_status_hint"], "監視エラー")
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["errors"])
        self.assertEqual(
            next(call for call in result["source_calls"] if call["name"] == "chimolog_github_normalized_json")["status"],
            "error",
        )

    def test_query_requires_all_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            page, direct, github = self.write_sources(directory)
            result = fetch_chimolog(
                page_file=page,
                direct_file=direct,
                github_file=github,
                query="SSD missing-token",
            )
        self.assertEqual(result["items"], [])
        self.assertEqual(result["source_status"], "ok")


if __name__ == "__main__":
    unittest.main()

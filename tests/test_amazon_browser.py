import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from amazon_browser import (
    PlaywrightCLI,
    acquire_pages,
    confirmation_from_page,
    model_from_title,
    parse_cli_json,
    parse_discount,
    parse_points_yen,
    parse_price_yen,
)


def page_result(**overrides):
    value = {
        "url": "https://www.amazon.co.jp/dp/B0TEST0001?th=1",
        "pageTitle": "Sample SSD - Amazon.co.jp",
        "captcha": False,
        "blocked": False,
        "title": "Sample SSD 2TB",
        "priceText": "￥19,800",
        "pointsText": "198ポイント (1%)",
        "couponText": "￥1,000 OFFクーポン",
        "registerDiscountText": None,
        "availability": "在庫あり",
        "seller": "Amazon.co.jp",
        "merchantInfo": "出荷元 / 販売元 Amazon.co.jp",
        "tabularRows": [],
        "cartable": True,
        "conditionText": "新品",
        "shippingText": "無料配送",
        "primeText": "",
        "buyboxText": "",
        "detailPairs": [
            {"label": "商品モデル番号", "value": "SSD-2000"},
            {"label": "保証", "value": "メーカー保証5年"},
        ],
        "featureBullets": [],
    }
    value.update(overrides)
    return value


class FakeRunner:
    def __init__(self, pages):
        self.pages = list(pages)
        self.commands = []

    def run(self, command, *arguments, json_output=False):
        self.commands.append((command, arguments, json_output))
        return ""

    def extract(self):
        return self.pages.pop(0)


class AmazonBrowserTest(unittest.TestCase):
    def test_amount_parsers_are_separate(self):
        self.assertEqual(parse_price_yen("￥19,800 税込"), 19800)
        self.assertEqual(parse_points_yen("獲得予定 1,980ポイント"), 1980)
        self.assertEqual(parse_discount("5% OFFクーポン"), (None, 5.0))
        self.assertEqual(parse_discount("￥1,000 OFFクーポン"), (1000, None))
        self.assertEqual(
            model_from_title("WD Black SN7100 2TB WDS200T4X0E-EC 国内正規品"),
            "WDS200T4X0E-EC",
        )

    def test_playwright_nested_json_output_is_decoded(self):
        output = json.dumps(
            {"result": json.dumps(json.dumps({"title": "Sample"}))}
        )
        self.assertEqual(parse_cli_json(output), {"title": "Sample"})

    def test_config_is_only_passed_when_opening_session(self):
        runner = object.__new__(PlaywrightCLI)
        runner.executable = "playwright-cli"
        runner.session = "test"
        runner.config = Path("/tmp/config.json")
        runner.timeout_seconds = 1
        runner.project_dir = Path("/tmp")
        with patch(
            "amazon_browser.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ) as mocked:
            runner.run("open", "about:blank")
            open_args = mocked.call_args.args[0]
            runner.run("goto", "https://example.com")
            goto_args = mocked.call_args.args[0]
        self.assertIn("--config=/tmp/config.json", open_args)
        self.assertNotIn("--config=/tmp/config.json", goto_args)

    def test_page_confirmation_does_not_apply_coupon_or_auto_trust_third_party(self):
        result = confirmation_from_page(
            "B0TEST0001",
            page_result(
                seller="Example Shop",
                merchantInfo="出荷元 / 販売元 Example Shop",
            ),
            "2026-07-15T10:00:00+09:00",
        )
        self.assertEqual(result["acquisition_status"], "ok")
        self.assertEqual(result["price_yen"], 19800)
        self.assertEqual(result["points_yen"], 198)
        self.assertEqual(result["coupon_discount_yen"], 1000)
        self.assertFalse(result["coupon_applied"])
        self.assertIsNone(result["seller_trusted"])
        self.assertTrue(result["seller_trust_requires_review"])
        self.assertEqual(result["shipper"], "Example Shop")
        self.assertEqual(result["shipping_yen"], 0)
        self.assertEqual(result["condition"], "新品")
        self.assertEqual(result["model"], "SSD-2000")

    def test_captcha_is_reported_without_attempting_bypass(self):
        result = confirmation_from_page(
            "B0TEST0001",
            page_result(captcha=True),
            "2026-07-15T10:00:00+09:00",
        )
        self.assertEqual(result["acquisition_status"], "captcha")
        self.assertIn("回避", result["error"])
        self.assertNotIn("price_yen", result)

    def test_anonymous_prime_price_is_not_adopted_as_regular_price(self):
        result = confirmation_from_page(
            "B0TEST0001",
            page_result(primeText="プライム限定価格"),
            "2026-07-15T10:00:00+09:00",
        )
        self.assertEqual(result["acquisition_status"], "ok")
        self.assertIsNone(result["price_yen"])
        self.assertEqual(result["prime_exclusive_price_yen"], 19800)
        self.assertIsNone(result["prime_eligible"])
        self.assertTrue(result["prime_exclusive"])
        self.assertTrue(any("通常価格として採用" in warning for warning in result["warnings"]))

    def test_acquire_pages_takes_snapshot_and_returns_nonempty_error(self):
        runner = FakeRunner([page_result(), page_result(captcha=True)])
        result = acquire_pages(
            ["B0TEST0001", "B0TEST0002"],
            runner=runner,
        )
        self.assertEqual(result["source_status"], "partial")
        self.assertTrue(result["required_source_failure"])
        self.assertEqual(result["monitor_status_hint"], "監視エラー")
        self.assertEqual(result["acquired_count"], 1)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(
            [command for command, _, _ in runner.commands].count("snapshot"),
            2,
        )
        self.assertEqual(runner.commands[-1][0], "close")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Confirm Amazon.co.jp product-page facts with a read-only Playwright session.

The helper never signs in, clicks purchase controls, attempts CAPTCHA solving, or
marks a third-party seller as trusted.  It produces a confirmation JSON that can
be consumed directly by amazon_price_checker.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from amazon_price_checker import AMAZON_SELLER_NAMES, extract_asin


DEFAULT_CONFIG = Path(__file__).with_name("playwright-cli.json")
DEFAULT_OUTPUT = Path("amazon_page_confirmations.json")
DEFAULT_MAX_ITEMS = 10


PAGE_EXTRACTOR = r"""async () => {
  const clean = (value) => {
    if (value === null || value === undefined) return null;
    const result = String(value).replace(/\s+/g, ' ').trim();
    return result || null;
  };
  const firstText = (selectors) => {
    for (const selector of selectors) {
      const element = document.querySelector(selector);
      const value = clean(element && (element.innerText || element.textContent));
      if (value) return value;
    }
    return null;
  };
  const texts = (selector, limit = 30) => Array.from(document.querySelectorAll(selector))
    .slice(0, limit)
    .map((element) => clean(element.innerText || element.textContent))
    .filter(Boolean);

  const bodyText = clean(document.body && document.body.innerText) || '';
  const captcha = Boolean(document.querySelector('#captchacharacters, form[action*="validateCaptcha"]'))
    || /ロボットではないことを確認|文字を入力してください/.test(bodyText.slice(0, 4000));
  const blocked = /Sorry! Something went wrong|申し訳ありませんが、お客様のリクエストの処理中に問題が発生しました/.test(bodyText.slice(0, 5000));

  const detailPairs = [];
  for (const row of document.querySelectorAll(
    '#productDetails_techSpec_section_1 tr, #productDetails_detailBullets_sections1 tr, '
    + '#productOverview_feature_div tr, #technicalSpecifications_section_1 tr'
  )) {
    const label = clean(row.querySelector('th, .a-span3, .a-span4')?.innerText);
    const value = clean(row.querySelector('td, .a-span9, .a-span8')?.innerText);
    if (label && value) detailPairs.push({ label, value });
  }
  for (const item of document.querySelectorAll('#detailBullets_feature_div li')) {
    const labelElement = item.querySelector('.a-text-bold');
    const label = clean(labelElement?.innerText || labelElement?.textContent)?.replace(/[:：]\s*$/, '');
    const whole = clean(item.innerText || item.textContent);
    const value = label && whole ? clean(whole.replace(label, '').replace(/^[:：]\s*/, '')) : null;
    if (label && value) detailPairs.push({ label, value });
  }

  const tabularRows = [];
  for (const container of document.querySelectorAll('#tabular-buybox .tabular-buybox-container, [id^="tabular-buybox-truncate-"]')) {
    const row = container.closest('.tabular-buybox-container') || container.parentElement;
    const label = firstTextFrom(row, ['.tabular-buybox-label', '.a-color-secondary']);
    const value = clean(container.innerText || container.textContent);
    if (label || value) tabularRows.push({ label, value });
  }
  function firstTextFrom(root, selectors) {
    if (!root) return null;
    for (const selector of selectors) {
      const element = root.querySelector(selector);
      const value = clean(element && (element.innerText || element.textContent));
      if (value) return value;
    }
    return null;
  }

  const cart = document.querySelector('#add-to-cart-button, input[name="submit.add-to-cart"]');
  const buyNow = document.querySelector('#buy-now-button, input[name="submit.buy-now"]');
  const enabled = (element) => Boolean(element)
    && !element.disabled
    && element.getAttribute('aria-disabled') !== 'true'
    && !String(element.className || '').includes('disabled');
  const buyboxText = firstText(['#desktop_buybox', '#buybox', '#apex_desktop']) || '';
  const couponCandidates = texts(
    '#couponText, [id*="coupon"] .a-color-success, [id*="coupon"] label, [class*="coupon"]',
    20
  ).filter((value) => /クーポン|coupon/i.test(value));
  const registerCandidates = buyboxText.split(/\n|。/)
    .map(clean)
    .filter((value) => value && /レジ.*割引|注文確定時.*割引|checkout.*discount/i.test(value));

  return JSON.stringify({
    url: location.href,
    pageTitle: document.title,
    captcha,
    blocked,
    title: firstText(['#productTitle', '#title']),
    priceText: firstText([
      '#corePrice_feature_div .a-price .a-offscreen',
      '#corePriceDisplay_desktop_feature_div .a-price .a-offscreen',
      '#apex_desktop .a-price .a-offscreen',
      '#desktop_buybox .a-price .a-offscreen'
    ]),
    pointsText: firstText([
      '#pointsInsideBuyBox_feature_div', '#points_feature_div',
      '[id*="pointsInsideBuyBox"]', '#loyalty-points'
    ]),
    couponText: couponCandidates[0] || null,
    registerDiscountText: registerCandidates[0] || null,
    availability: firstText(['#availability', '#outOfStock', '#availabilityInsideBuyBox_feature_div']),
    seller: firstText(['#sellerProfileTriggerId', '#merchant-info a', '#merchant-info']),
    merchantInfo: firstText(['#merchantInfoFeature_feature_div', '#merchant-info']),
    tabularRows,
    cartPresent: Boolean(cart),
    buyNowPresent: Boolean(buyNow),
    cartable: enabled(cart) || enabled(buyNow),
    conditionText: firstText([
      '#condition-value', '#offerDisplayFeatures_feature_div #condition-value',
      '#newAccordionRow .a-size-base', '#usedAccordionRow .a-size-base',
      '#buybox-see-all-buying-choices-announce'
    ]),
    shippingText: firstText([
      '#mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE',
      '#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE',
      '#deliveryBlockMessage', '#shippingMessageInsideBuyBox_feature_div'
    ]),
    primeText: firstText([
      '#primeSavingsUpsellCaptionFeature', '#prime-exclusive-price',
      '#primeExclusivePricingMessage', '#primeBadge_feature_div'
    ]),
    buyboxText,
    detailPairs: detailPairs.slice(0, 60),
    featureBullets: texts('#feature-bullets li, #important-information li', 40)
  });
}"""


class BrowserAcquisitionError(RuntimeError):
    """A predictable Playwright CLI or page acquisition failure."""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def int_from_text(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    match = re.search(r"\d[\d,]*", str(value))
    return int(match.group(0).replace(",", "")) if match else None


def parse_price_yen(value: Any) -> int | None:
    text = str(value or "")
    match = re.search(r"[￥¥]\s*(\d[\d,]*)", text)
    return int(match.group(1).replace(",", "")) if match else None


def parse_points_yen(value: Any) -> int | None:
    text = str(value or "")
    patterns = (
        r"(\d[\d,]*)\s*(?:ポイント|pt)",
        r"(?:ポイント|pt)\s*[:：]?\s*(\d[\d,]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1).replace(",", ""))
    return None


def parse_discount(value: Any) -> tuple[int | None, float | None]:
    text = str(value or "")
    money = re.search(r"[￥¥]\s*(\d[\d,]*)", text)
    if not money:
        money = re.search(r"(\d[\d,]*)\s*円", text)
    percent = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return (
        int(money.group(1).replace(",", "")) if money else None,
        float(percent.group(1)) if percent else None,
    )


def normalize_condition(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "整備済" in text or "refurb" in text.lower():
        return "整備済み"
    if "中古" in text or "used" in text.lower():
        return "中古"
    if "開封" in text or "open box" in text.lower():
        return "開封品"
    if "新品" in text or text.lower() == "new":
        return "新品"
    return None


def detail_value(raw: dict[str, Any], labels: Iterable[str]) -> str | None:
    candidates = tuple(label.lower() for label in labels)
    for pair in raw.get("detailPairs") or []:
        if not isinstance(pair, dict):
            continue
        label = str(pair.get("label") or "").strip().lower()
        value = str(pair.get("value") or "").strip()
        if value and any(candidate in label for candidate in candidates):
            return value
    return None


def warranty_value(raw: dict[str, Any]) -> str | None:
    value = detail_value(raw, ("保証", "warranty"))
    if value:
        return value
    for bullet in raw.get("featureBullets") or []:
        text = str(bullet or "").strip()
        if "保証" in text or "warranty" in text.lower():
            return text
    return None


def model_from_title(value: Any) -> str | None:
    """Extract only strong part-number-shaped tokens; avoid broad fuzzy guesses."""
    text = str(value or "")
    candidates = re.findall(r"(?<![A-Z0-9])(?=[A-Z0-9-]{8,}(?![A-Z0-9]))(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9]+(?:-[A-Z0-9]+)+(?![A-Z0-9])", text.upper())
    ignored = {"M-2-2280", "PCI-E-4", "PCI-E-5"}
    return next((candidate for candidate in reversed(candidates) if candidate not in ignored), None)


def seller_shipper(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    seller = str(raw.get("seller") or "").strip() or None
    shipper: str | None = None
    for row in raw.get("tabularRows") or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "")
        value = str(row.get("value") or "").strip() or None
        if not value:
            continue
        if "出荷元" in label:
            shipper = value
        if "販売元" in label:
            seller = value
    merchant = str(raw.get("merchantInfo") or "").strip()
    if merchant and seller is None:
        lines = [line.strip() for line in merchant.splitlines() if line.strip()]
        if lines:
            seller = lines[-1]
    if seller and re.search(r"出荷元\s*/\s*販売元", merchant):
        shipper = seller
    return seller, shipper


def shipping_value(raw: dict[str, Any]) -> tuple[bool, int | None]:
    text = str(raw.get("shippingText") or "").strip()
    if not text:
        return False, None
    if "配送料無料" in text or "無料配送" in text or "送料無料" in text:
        return True, 0
    match = re.search(r"(?:送料|配送料)[^￥¥\d]{0,8}[￥¥]?\s*(\d[\d,]*)\s*円?", text)
    if match:
        return True, int(match.group(1).replace(",", ""))
    return False, None


def is_amazon_seller(value: Any) -> bool:
    return str(value or "").strip().lower() in AMAZON_SELLER_NAMES


def confirmation_from_page(asin: str, raw: dict[str, Any], confirmed_at: str) -> dict[str, Any]:
    url = str(raw.get("url") or f"https://www.amazon.co.jp/dp/{asin}")
    base: dict[str, Any] = {
        "asin": asin,
        "confirmed_at": confirmed_at,
        "product_url": url,
        "page_title": raw.get("pageTitle"),
    }
    if raw.get("captcha"):
        return {
            **base,
            "acquisition_status": "captcha",
            "error": "AmazonがCAPTCHAを表示したため確認を中止しました。回避・自動解答は行いません",
        }
    if raw.get("blocked"):
        return {
            **base,
            "acquisition_status": "blocked",
            "error": "Amazonがエラーページを返したため商品情報を確認できませんでした",
        }

    title = str(raw.get("title") or "").strip() or None
    price_yen = parse_price_yen(raw.get("priceText"))
    availability = str(raw.get("availability") or "").strip() or None
    cartable = bool(raw.get("cartable"))
    if not title:
        return {
            **base,
            "acquisition_status": "acquisition_failed",
            "error": "Amazon商品ページの商品名を取得できませんでした",
        }

    seller, shipper = seller_shipper(raw)
    coupon_yen, coupon_percent = parse_discount(raw.get("couponText"))
    register_yen, register_percent = parse_discount(raw.get("registerDiscountText"))
    shipping_checked, shipping_yen = shipping_value(raw)
    condition = normalize_condition(raw.get("conditionText"))
    model = detail_value(
        raw,
        ("商品モデル番号", "製品型番", "メーカー型番", "モデル", "model", "part number"),
    ) or model_from_title(title)
    warranty = warranty_value(raw)
    prime_text = " ".join(
        str(value or "") for value in (raw.get("primeText"), raw.get("buyboxText"))
    )
    prime_exclusive = bool(re.search(r"プライム限定価格|Prime限定", prime_text, re.IGNORECASE))
    points_yen = parse_points_yen(raw.get("pointsText"))
    if points_yen is None:
        points_yen = 0

    unavailable = not cartable and (
        price_yen is None
        or any(
            marker in str(availability or "")
            for marker in ("在庫切れ", "在庫なし", "お取り扱いしておりません", "入荷時期は未定")
        )
    )
    status = "confirmed_unavailable" if unavailable else "ok"
    error = None
    if price_yen is None and not unavailable:
        status = "acquisition_failed"
        error = "Amazon商品ページの現在価格を取得できませんでした"

    warnings: list[str] = []
    if seller and not is_amazon_seller(seller):
        warnings.append("第三者販売元のため信頼性は未判定です")
    if not seller:
        warnings.append("販売元を確認できませんでした")
    if not shipper:
        warnings.append("発送元を確認できませんでした")
    if not shipping_checked:
        warnings.append("送料を確定できませんでした")
    if condition is None:
        warnings.append("商品状態を確定できませんでした")
    if raw.get("couponText"):
        warnings.append("クーポンは表示のみ確認し、選択・適用していません")
    if raw.get("registerDiscountText"):
        warnings.append("レジ割引は表示のみ確認し、注文確定画面で検証していません")

    return {
        **base,
        "acquisition_status": status,
        "error": error,
        "title": title,
        "price_yen": price_yen,
        "points_yen": points_yen,
        "coupon_checked": True,
        "coupon_text": raw.get("couponText"),
        "coupon_discount_yen": coupon_yen,
        "coupon_discount_percent": coupon_percent,
        "coupon_applied": False,
        "register_discount_checked": True,
        "register_discount_text": raw.get("registerDiscountText"),
        "register_discount_yen": register_yen,
        "register_discount_percent": register_percent,
        "register_discount_applied": False,
        "shipping_checked": shipping_checked,
        "shipping_text": raw.get("shippingText"),
        "shipping_yen": shipping_yen,
        "seller": seller,
        "seller_trusted": True if is_amazon_seller(seller) else None,
        "seller_trust_requires_review": bool(seller and not is_amazon_seller(seller)),
        "shipper": shipper,
        "availability": availability,
        "cartable": cartable,
        "condition": condition,
        "condition_matches": None,
        "model": model,
        "model_matches": None,
        "warranty": warranty,
        "prime_exclusive": prime_exclusive,
        "prime_eligible": None,
        "warnings": warnings,
        "page_evidence": {
            "price_text": raw.get("priceText"),
            "points_text": raw.get("pointsText"),
            "merchant_info": raw.get("merchantInfo"),
            "condition_text": raw.get("conditionText"),
        },
    }


def parse_cli_json(stdout: str) -> Any:
    text = stdout.strip()
    candidates = [text] + [line.strip() for line in reversed(text.splitlines()) if line.strip()]
    payload: Any = None
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    if payload is None:
        raise BrowserAcquisitionError("Playwright CLIからJSON出力を取得できませんでした")
    if isinstance(payload, dict) and "result" in payload:
        payload = payload["result"]
    for _ in range(3):
        if not isinstance(payload, str):
            break
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise BrowserAcquisitionError("Playwrightのページ抽出結果が不正なJSONです") from exc
    return payload


class PlaywrightCLI:
    def __init__(
        self,
        *,
        session: str,
        config: Path,
        timeout_seconds: float,
        project_dir: Path,
    ) -> None:
        local = project_dir / "node_modules" / ".bin" / "playwright-cli"
        executable = str(local) if local.exists() else shutil.which("playwright-cli")
        if not executable:
            raise BrowserAcquisitionError(
                "playwright-cliが見つかりません。`npm install` と "
                "`npm run browser:install` を実行してください"
            )
        self.executable = executable
        self.session = re.sub(r"[^A-Za-z0-9_-]", "-", session)
        self.config = config.resolve()
        self.timeout_seconds = timeout_seconds
        self.project_dir = project_dir

    def run(self, command: str, *arguments: str, json_output: bool = False) -> str:
        args = [
            self.executable,
            f"-s={self.session}",
        ]
        # In playwright-cli 0.1.x --config belongs to session creation.  Passing
        # it to goto/eval/close is rejected as an unknown command option.
        if command == "open":
            args.append(f"--config={self.config}")
        if json_output:
            args.append("--json")
        args.extend([command, *arguments])
        environment = os.environ.copy()
        environment.setdefault(
            "PLAYWRIGHT_MCP_OUTPUT_DIR",
            str((self.project_dir / "output" / "playwright").resolve()),
        )
        try:
            completed = subprocess.run(
                args,
                cwd=self.project_dir,
                env=environment,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BrowserAcquisitionError(
                f"Playwright CLIが{self.timeout_seconds:g}秒でタイムアウトしました"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise BrowserAcquisitionError(
                f"Playwright CLI {command} が失敗しました: {detail[-1000:]}"
            )
        return completed.stdout

    def extract(self) -> dict[str, Any]:
        result = parse_cli_json(self.run("eval", PAGE_EXTRACTOR, json_output=True))
        if not isinstance(result, dict):
            raise BrowserAcquisitionError("Playwrightのページ抽出結果がオブジェクトではありません")
        return result


def values_from_candidate_file(path: Path) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = raw.get("items") or raw.get("products") or raw.get("asins") or []
    else:
        raise ValueError("候補JSONのルートはオブジェクトまたは配列にしてください")
    if not isinstance(rows, list):
        raise ValueError("候補JSONの items/products/asins は配列にしてください")
    result: list[str] = []
    for row in rows:
        if isinstance(row, str):
            result.append(row)
        elif isinstance(row, dict):
            value = row.get("asin") or row.get("product_url") or row.get("url")
            if value:
                result.append(str(value))
    return result


def unique_targets(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        asin = extract_asin(value)
        if asin not in seen:
            result.append(asin)
            seen.add(asin)
    return result


def acquire_pages(
    asins: list[str],
    *,
    runner: PlaywrightCLI,
    keep_session: bool = False,
) -> dict[str, Any]:
    started_at = now_iso()
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    opened = False
    try:
        runner.run("open", "about:blank")
        opened = True
        for asin in asins:
            confirmed_at = now_iso()
            try:
                runner.run("goto", f"https://www.amazon.co.jp/dp/{asin}?th=1")
                snapshot_error = None
                try:
                    runner.run("snapshot")
                except BrowserAcquisitionError as exc:
                    snapshot_error = str(exc)
                raw = runner.extract()
                item = confirmation_from_page(asin, raw, confirmed_at)
                if snapshot_error:
                    item.setdefault("warnings", []).append(
                        "Playwrightスナップショット保存失敗: " + snapshot_error
                    )
                items.append(item)
                if item.get("acquisition_status") in {"captcha", "blocked", "acquisition_failed"}:
                    errors.append(
                        {
                            "asin": asin,
                            "stage": "amazon_product_page",
                            "message": item.get("error") or "Amazon商品ページ確認に失敗しました",
                        }
                    )
            except BrowserAcquisitionError as exc:
                message = str(exc)
                items.append(
                    {
                        "asin": asin,
                        "confirmed_at": confirmed_at,
                        "product_url": f"https://www.amazon.co.jp/dp/{asin}",
                        "acquisition_status": "acquisition_failed",
                        "error": message,
                    }
                )
                errors.append(
                    {"asin": asin, "stage": "amazon_product_page", "message": message}
                )
    finally:
        if opened and not keep_session:
            try:
                runner.run("close")
            except BrowserAcquisitionError as exc:
                errors.append({"stage": "browser_close", "message": str(exc)})

    acquired = sum(
        item.get("acquisition_status") in {"ok", "confirmed_unavailable"} for item in items
    )
    source_status = "ok" if not errors else ("partial" if acquired else "error")
    return {
        "schema_version": 1,
        "source": "Amazon.co.jp official product pages via Playwright CLI",
        "source_status": source_status,
        "required_source_failure": source_status == "error",
        "monitor_status_hint": "監視エラー" if source_status == "error" else None,
        "started_at": started_at,
        "finished_at": now_iso(),
        "requested_count": len(asins),
        "acquired_count": acquired,
        "error_count": len(errors),
        "errors": errors,
        "safety": {
            "anonymous_session": True,
            "read_only": True,
            "cart_or_purchase_clicks": False,
            "captcha_bypass": False,
            "third_party_seller_auto_trust": False,
        },
        "items": items,
    }


def error_result(message: str, requested_count: int = 0) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "Amazon.co.jp official product pages via Playwright CLI",
        "source_status": "error",
        "required_source_failure": True,
        "monitor_status_hint": "監視エラー",
        "started_at": now_iso(),
        "finished_at": now_iso(),
        "requested_count": requested_count,
        "acquired_count": 0,
        "error_count": 1,
        "errors": [{"stage": "browser_setup", "message": message}],
        "items": [],
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Amazon.co.jp商品ページを匿名・読み取り専用のPlaywrightで確認します"
    )
    parser.add_argument("targets", nargs="*", help="ASINまたはAmazon商品URL")
    parser.add_argument("--candidates-file", type=Path, help="items/products/asinsを含む候補JSON")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    parser.add_argument("--timeout", type=float, default=60.0, help="CLIコマンドごとの秒数")
    parser.add_argument("--session", default=f"amazon-price-checker-{os.getpid()}")
    parser.add_argument("--keep-session", action="store_true", help="デバッグ用にブラウザを閉じない")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: dict[str, Any]
    try:
        values = list(args.targets)
        if args.candidates_file:
            values.extend(values_from_candidate_file(args.candidates_file))
        asins = unique_targets(values)
        if not asins:
            raise ValueError("確認対象のASINがありません")
        if args.max_items < 1:
            raise ValueError("--max-items は1以上にしてください")
        truncated = len(asins) > args.max_items
        selected = asins[: args.max_items]
        project_dir = Path(__file__).resolve().parent
        runner = PlaywrightCLI(
            session=args.session,
            config=args.config,
            timeout_seconds=args.timeout,
            project_dir=project_dir,
        )
        result = acquire_pages(selected, runner=runner, keep_session=args.keep_session)
        result["selection"] = {
            "input_count": len(asins),
            "selected_count": len(selected),
            "max_items": args.max_items,
            "truncated": truncated,
        }
        if truncated:
            result.setdefault("warnings", []).append(
                f"安全な短時間確認のため{len(asins) - len(selected)}件を次回へ繰り越しました"
            )
    except (BrowserAcquisitionError, OSError, ValueError, json.JSONDecodeError) as exc:
        result = error_result(str(exc), requested_count=len(args.targets))
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("source_status") == "error" else 0


if __name__ == "__main__":
    sys.exit(main())

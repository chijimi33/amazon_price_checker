#!/usr/bin/env python3
"""Fetch and reconcile Chimolog's public Amazon candidate data.

This module never treats the public JSON as final Amazon offer confirmation.
It checks the public price page, the site's current data file, and the required
GitHub-normalized mirror, then preserves source differences for a later
Amazon.co.jp product-page confirmation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


PRICE_PAGE_URL = "https://chimolog.co/wp-content/price/"
DIRECT_DATA_URL = "https://chimolog.co/wp-content/price/data/products.json"
GITHUB_DATA_URL = (
    "https://raw.githubusercontent.com/chijimi33/Chimolog-price-tool/"
    "main/public/chimolog_products.json"
)
USER_AGENT = "Agent/amazon_price_checker/1.0 (+https://github.com/chijimi33/amazon_price_checker)"


class SourceError(RuntimeError):
    """A required public source could not be acquired or parsed."""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def int_value(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def fetch_bytes(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"{url}: {exc}") from exc


def load_bytes(path: Path | None, url: str, timeout: float) -> bytes:
    if path:
        try:
            return path.read_bytes()
        except OSError as exc:
            raise SourceError(f"{path}: {exc}") from exc
    return fetch_bytes(url, timeout)


def decode_json(content: bytes, source: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError(f"{source}: JSONを解析できません: {exc}") from exc
    if not isinstance(value, dict):
        raise SourceError(f"{source}: JSONルートはオブジェクトである必要があります")
    return value


def normalize_asin(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if re.fullmatch(r"[A-Z0-9]{10}", text) else None


def normalize_direct(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    products = raw.get("products")
    if not isinstance(products, list):
        raise SourceError("ちもろぐ本体JSONに products 配列がありません")
    result: list[dict[str, Any]] = []
    for item in products:
        if not isinstance(item, dict):
            continue
        asin = normalize_asin(item.get("asin"))
        if not asin:
            continue
        discount = item.get("discount") or {}
        points = item.get("points") or {}
        price = int_value(item.get("price"))
        reference = int_value(discount.get("ref_high")) if isinstance(discount, dict) else None
        discount_rate = discount.get("rate_percent") if isinstance(discount, dict) else None
        result.append(
            {
                "asin": asin,
                "title": str(item.get("title") or "").strip() or None,
                "category": str(item.get("category") or "").strip(),
                "themes": list(item.get("themes") or []),
                "theme_labels": [],
                "price_yen": price,
                "reference_high_yen": reference,
                "discount_amount_yen": (
                    reference - price
                    if reference is not None and price is not None and reference >= price
                    else None
                ),
                "discount_rate_percent": discount_rate,
                "is_discounted": bool(discount),
                "points_total": int_value(points.get("total")) or 0,
                "points_rate_percent": points.get("rate_percent"),
                "fetched_at": item.get("fetched_at"),
                "product_url": f"https://www.amazon.co.jp/dp/{asin}",
                "affiliate_url": item.get("affiliate_url"),
                "review_url": item.get("review_url") or None,
                "image_url": item.get("image_url"),
            }
        )
    return result, raw.get("meta") if isinstance(raw.get("meta"), dict) else {}


def normalize_github(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items = raw.get("items")
    if not isinstance(items, list):
        raise SourceError("GitHub正規化JSONに items 配列がありません")
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        asin = normalize_asin(item.get("asin"))
        if not asin:
            continue
        result.append(
            {
                "asin": asin,
                "title": item.get("title"),
                "category": str(item.get("category") or "").strip(),
                "themes": list(item.get("themes") or []),
                "theme_labels": list(item.get("theme_labels") or []),
                "price_yen": int_value(item.get("price_yen")),
                "reference_high_yen": int_value(item.get("reference_high_yen")),
                "discount_amount_yen": int_value(item.get("discount_amount_yen")),
                "discount_rate_percent": item.get("discount_rate_percent"),
                "is_discounted": item.get("is_discounted") is True,
                "points_total": int_value(item.get("points_total")) or 0,
                "points_rate_percent": item.get("points_rate_percent"),
                "fetched_at": item.get("fetched_at"),
                "product_url": f"https://www.amazon.co.jp/dp/{asin}",
                "affiliate_url": item.get("affiliate_url") or item.get("product_url"),
                "review_url": item.get("review_url"),
                "image_url": item.get("image_url"),
            }
        )
    metadata = {
        "source_url": raw.get("source_url"),
        "data_url": raw.get("data_url"),
        "fetched_at": raw.get("fetched_at"),
        "site": raw.get("site"),
        "count": raw.get("count"),
        "discount_count": raw.get("discount_count"),
    }
    return result, metadata


def compare_values(direct: dict[str, Any] | None, github: dict[str, Any] | None) -> dict[str, Any]:
    direct = direct or {}
    github = github or {}
    direct_price = int_value(direct.get("price_yen"))
    github_price = int_value(github.get("price_yen"))
    direct_points = int_value(direct.get("points_total"))
    github_points = int_value(github.get("points_total"))
    return {
        "direct": {
            "price_yen": direct_price,
            "points_total": direct_points,
            "fetched_at": direct.get("fetched_at"),
        },
        "github_normalized": {
            "price_yen": github_price,
            "points_total": github_points,
            "fetched_at": github.get("fetched_at"),
        },
        "price_difference_direct_minus_github_yen": (
            direct_price - github_price
            if direct_price is not None and github_price is not None
            else None
        ),
        "points_difference_direct_minus_github": (
            direct_points - github_points
            if direct_points is not None and github_points is not None
            else None
        ),
        "has_difference": (
            direct_price != github_price or direct_points != github_points
            if direct and github
            else True
        ),
    }


def merge_items(
    direct_items: list[dict[str, Any]],
    github_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    direct_by_asin = {item["asin"]: item for item in direct_items}
    github_by_asin = {item["asin"]: item for item in github_items}
    ordered_asins = list(direct_by_asin)
    ordered_asins.extend(asin for asin in github_by_asin if asin not in direct_by_asin)
    result: list[dict[str, Any]] = []
    for asin in ordered_asins:
        direct = direct_by_asin.get(asin)
        github = github_by_asin.get(asin)
        adopted = direct or github
        if not adopted:
            continue
        price = int_value(adopted.get("price_yen"))
        points = int_value(adopted.get("points_total")) or 0
        result.append(
            {
                **adopted,
                "effective_price_reference_yen": (
                    max(price - points, 0) if price is not None else None
                ),
                "adopted_public_source": (
                    "chimolog_direct_json" if direct else "chimolog_github_normalized_json"
                ),
                "source_comparison": compare_values(direct, github),
            }
        )
    return result


def error_record(stage: str, source: str, exc: Exception) -> dict[str, Any]:
    return {
        "stage": stage,
        "source": source,
        "type": type(exc).__name__,
        "message": str(exc),
    }


def acquire_one(
    *,
    name: str,
    url: str,
    file: Path | None,
    timeout: float,
    parser: Callable[[bytes], Any],
    required: bool,
    errors: list[dict[str, Any]],
    source_calls: list[dict[str, Any]],
) -> Any:
    checked_at = now_iso()
    source = str(file) if file else url
    try:
        content = load_bytes(file, url, timeout)
        value = parser(content)
        source_calls.append(
            {
                "name": name,
                "url": url,
                "input": source,
                "required": required,
                "status": "ok",
                "checked_at": checked_at,
                "bytes": len(content),
            }
        )
        return value
    except SourceError as exc:
        errors.append(error_record(name, source, exc))
        source_calls.append(
            {
                "name": name,
                "url": url,
                "input": source,
                "required": required,
                "status": "error",
                "checked_at": checked_at,
                "bytes": 0,
            }
        )
        return None


def fetch_chimolog(
    *,
    timeout: float = 20.0,
    page_file: Path | None = None,
    direct_file: Path | None = None,
    github_file: Path | None = None,
    categories: list[str] | None = None,
    query: str | None = None,
    discounted_only: bool = False,
    strict_sources: bool = False,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    source_calls: list[dict[str, Any]] = []
    page = acquire_one(
        name="chimolog_price_page",
        url=PRICE_PAGE_URL,
        file=page_file,
        timeout=timeout,
        parser=lambda value: value.decode("utf-8", errors="replace"),
        required=strict_sources,
        errors=errors,
        source_calls=source_calls,
    )
    direct_raw = acquire_one(
        name="chimolog_direct_json",
        url=DIRECT_DATA_URL,
        file=direct_file,
        timeout=timeout,
        parser=lambda value: decode_json(value, str(direct_file or DIRECT_DATA_URL)),
        required=strict_sources,
        errors=errors,
        source_calls=source_calls,
    )
    github_raw = acquire_one(
        name="chimolog_github_normalized_json",
        url=GITHUB_DATA_URL,
        file=github_file,
        timeout=timeout,
        parser=lambda value: decode_json(value, str(github_file or GITHUB_DATA_URL)),
        required=strict_sources,
        errors=errors,
        source_calls=source_calls,
    )

    direct_items: list[dict[str, Any]] = []
    github_items: list[dict[str, Any]] = []
    direct_meta: dict[str, Any] = {}
    github_meta: dict[str, Any] = {}
    if direct_raw is not None:
        try:
            direct_items, direct_meta = normalize_direct(direct_raw)
        except SourceError as exc:
            errors.append(error_record("chimolog_direct_normalize", DIRECT_DATA_URL, exc))
    if github_raw is not None:
        try:
            github_items, github_meta = normalize_github(github_raw)
        except SourceError as exc:
            errors.append(error_record("chimolog_github_normalize", GITHUB_DATA_URL, exc))

    items = merge_items(direct_items, github_items)
    total_before_filter = len(items)
    category_set = {value.casefold() for value in categories or []}
    if category_set:
        items = [item for item in items if str(item.get("category") or "").casefold() in category_set]
    query_tokens = str(query or "").casefold().split()
    if query_tokens:
        items = [
            item
            for item in items
            if all(
                token in " ".join(
                    [
                        str(item.get("title") or ""),
                        str(item.get("category") or ""),
                        " ".join(item.get("themes") or []),
                    ]
                ).casefold()
                for token in query_tokens
            )
        ]
    if discounted_only:
        items = [item for item in items if item.get("is_discounted") is True]

    required_source_failure = strict_sources and any(
        call["status"] != "ok" for call in source_calls
    )
    if not direct_items and not github_items:
        source_status = "error"
    elif required_source_failure or errors:
        source_status = "partial"
    else:
        source_status = "ok"
    return {
        "schema_version": 1,
        "source": "Chimolog public price page / direct JSON / GitHub normalized JSON",
        "source_status": source_status,
        "required_source_failure": required_source_failure,
        "monitor_status_hint": "監視エラー" if required_source_failure else None,
        "fetched_at": now_iso(),
        "source_calls": source_calls,
        "errors": errors,
        "metadata": {
            "price_page_title_found": bool(page and "Amazon" in page),
            "direct": direct_meta,
            "github_normalized": github_meta,
            "public_item_count_before_filter": total_before_filter,
            "selected_item_count": len(items),
        },
        "limitations": [
            "公開JSONのprice_yenは候補抽出用の補助価格で、Amazon公式商品ページ価格ではない",
            "points_totalは参考実質価格計算に使うが、キャンペーン・支払方法依存ポイントを含む保証はない",
            "クーポン、Prime限定価格、販売元、発送元、在庫、状態、型番、保証はAmazon公式商品ページで再確認する",
            "公開JSONだけで今年最安値を確定しない",
        ],
        "assistant_handoff": {
            "is_final_notification_decision": False,
            "instruction": (
                "strict_sources指定時にrequired_source_failure=trueなら空応答にせず監視エラー。"
                "各候補はAmazon公式商品ページで再確認してから通知判定する。"
            ),
        },
        "items": items,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ちもろぐ公開ページと2つの公開JSONを取得・比較します",
    )
    parser.add_argument("--page-file", type=Path, help="価格ページHTMLを使うオフラインテスト")
    parser.add_argument("--direct-file", type=Path, help="ちもろぐ本体JSONを使うオフラインテスト")
    parser.add_argument("--github-file", type=Path, help="GitHub正規化JSONを使うオフラインテスト")
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--query")
    parser.add_argument("--discounted-only", action="store_true")
    parser.add_argument(
        "--strict-sources",
        action="store_true",
        help="3ソースのいずれかの失敗を監視エラー扱いにする",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--indent", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    result = fetch_chimolog(
        timeout=args.timeout,
        page_file=args.page_file,
        direct_file=args.direct_file,
        github_file=args.github_file,
        categories=args.category,
        query=args.query,
        discounted_only=args.discounted_only,
        strict_sources=args.strict_sources,
    )
    text = json.dumps(result, ensure_ascii=False, indent=args.indent) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 1 if (
        result["source_status"] == "error"
        or result["required_source_failure"]
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Amazon.co.jp offer-data helper for a PC-parts deal-monitoring task.

The live path uses Amazon's official Creators API. Product-page-only facts
(coupon, shipper and cart availability) can be supplied as manually verified
confirmations. Confirmed page values always win and API/page differences remain
visible in the output.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from quality_catalog import (
    DEFAULT_CATALOG as DEFAULT_QUALITY_CATALOG,
    attach_catalog_evaluation,
    load_catalog as load_quality_catalog,
    profile_by_id as quality_profile_by_id,
)


SCHEMA_VERSION = 1
DEFAULT_MARKETPLACE = "www.amazon.co.jp"
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)
ASIN_URL_PATTERNS = [
    re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE),
    re.compile(r"[?&]asin=([A-Z0-9]{10})(?:&|$)", re.IGNORECASE),
]

OFFER_RESOURCES = [
    "itemInfo.title",
    "itemInfo.byLineInfo",
    "itemInfo.features",
    "itemInfo.manufactureInfo",
    "itemInfo.productInfo",
    "itemInfo.technicalInfo",
    "offersV2.listings.availability",
    "offersV2.listings.condition",
    "offersV2.listings.dealDetails",
    "offersV2.listings.isBuyBoxWinner",
    "offersV2.listings.loyaltyPoints",
    "offersV2.listings.merchantInfo",
    "offersV2.listings.price",
    "offersV2.listings.type",
]

AVAILABLE_TYPES = {"IN_STOCK", "IN_STOCK_SCARCE", "LEADTIME", "PREORDER", "AVAILABLE_DATE"}
UNAVAILABLE_TYPES = {"OUT_OF_STOCK", "UNAVAILABLE"}
OUT_OF_STOCK_TEXT = (
    "在庫切れ",
    "在庫なし",
    "現在お取り扱いしておりません",
    "一時的に在庫切れ",
    "入荷時期は未定",
)
AMAZON_SELLER_NAMES = {
    "amazon.co.jp",
    "amazon japan g.k.",
    "アマゾンジャパン合同会社",
}
PAGE_CHECK_FIELDS = [
    "price_yen",
    "points_yen",
    "coupon_checked",
    "register_discount_checked",
    "shipping_yen",
    "seller",
    "seller_trusted",
    "shipper",
    "availability",
    "cartable",
    "condition",
    "model",
    "warranty",
    "prime_exclusive",
]


class CheckerError(RuntimeError):
    """An expected configuration or acquisition error."""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def dict_get(value: Any, *keys: str) -> Any:
    if not isinstance(value, dict):
        return None
    for key in keys:
        if key in value:
            return value[key]
    return None


def int_value(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def display_value(value: Any) -> str | None:
    if isinstance(value, dict):
        value = dict_get(value, "displayValue", "DisplayValue")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_asin(value: str) -> str:
    candidate = value.strip()
    if ASIN_RE.fullmatch(candidate):
        return candidate.upper()
    for pattern in ASIN_URL_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return match.group(1).upper()
    raise ValueError(f"ASINを判別できません: {value}")


def unique_asins(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        asin = extract_asin(value)
        if asin not in seen:
            seen.add(asin)
            result.append(asin)
    return result


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(path: Path | None, positional: list[str]) -> dict[str, Any]:
    raw = load_json(path) if path else {}
    if not isinstance(raw, dict):
        raise ValueError("設定JSONのルートはオブジェクトである必要があります")
    asin_values = list(raw.get("asins") or []) + positional
    searches = raw.get("searches") or []
    if not isinstance(searches, list) or not all(isinstance(value, dict) for value in searches):
        raise ValueError("設定JSONの searches はオブジェクトの配列である必要があります")
    return {
        "asins": unique_asins(str(value) for value in asin_values),
        "searches": searches,
        "marketplace": str(raw.get("marketplace") or DEFAULT_MARKETPLACE),
    }


def require_credentials() -> dict[str, str]:
    mapping = {
        "credential_id": "AMAZON_CREATORS_CREDENTIAL_ID",
        "credential_secret": "AMAZON_CREATORS_CREDENTIAL_SECRET",
        "version": "AMAZON_CREATORS_VERSION",
        "partner_tag": "AMAZON_CREATORS_PARTNER_TAG",
    }
    result = {key: os.environ.get(name, "").strip() for key, name in mapping.items()}
    missing = [mapping[key] for key, value in result.items() if not value]
    if missing:
        raise CheckerError("未設定の環境変数: " + ", ".join(missing))
    return result


def import_official_sdk() -> tuple[Any, Any, Any, Any]:
    try:
        from creatorsapi_python_sdk.api.default_api import DefaultApi
        from creatorsapi_python_sdk.api_client import ApiClient
        from creatorsapi_python_sdk.models.get_items_request_content import GetItemsRequestContent
        from creatorsapi_python_sdk.models.search_items_request_content import SearchItemsRequestContent
    except ImportError as exc:
        raise CheckerError(
            "Amazon公式SDKがありません。"
            " `python3 -m pip install -r requirements.txt` を実行してください"
        ) from exc
    return ApiClient, DefaultApi, GetItemsRequestContent, SearchItemsRequestContent


def error_record(stage: str, exc: Any, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    value = {
        "stage": stage,
        "type": type(exc).__name__ if isinstance(exc, BaseException) else "ApiError",
        "message": str(exc),
    }
    if context:
        value["context"] = context
    return value


def fetch_official_data(
    *,
    asins: list[str],
    searches: list[dict[str, Any]],
    marketplace: str,
    request_timeout: float,
    request_delay: float,
) -> dict[str, Any]:
    """Fetch watchlist and discovery items, preserving every source failure."""
    credentials = require_credentials()
    ApiClient, DefaultApi, GetItemsRequestContent, SearchItemsRequestContent = import_official_sdk()
    client = ApiClient(
        credential_id=credentials["credential_id"],
        credential_secret=credentials["credential_secret"],
        version=credentials["version"],
    )
    api = DefaultApi(client)
    item_by_asin: dict[str, dict[str, Any]] = {}
    discoveries: dict[str, list[str]] = {}
    errors: list[dict[str, Any]] = []
    source_calls: list[dict[str, Any]] = []

    calls: list[tuple[str, Any]] = []
    for batch in chunked(asins, 10):
        calls.append(("get_items", batch))
    for index, search in enumerate(searches):
        calls.append(("search_items", (index, search)))

    for call_index, (operation, payload) in enumerate(calls):
        try:
            if operation == "get_items":
                batch = payload
                request = GetItemsRequestContent(
                    partner_tag=credentials["partner_tag"],
                    item_ids=batch,
                    resources=OFFER_RESOURCES,
                    currency_of_preference="JPY",
                    languages_of_preference=["ja_JP"],
                )
                response = api.get_items(
                    x_marketplace=marketplace,
                    get_items_request_content=request,
                    _request_timeout=request_timeout,
                )
                data = response.to_dict()
                container = dict_get(data, "itemsResult", "ItemsResult") or {}
                items = dict_get(container, "items", "Items") or []
                context = {"operation": operation, "asins": batch}
            else:
                search_index, search = payload
                if not str(search.get("keywords") or "").strip():
                    raise ValueError("searches[].keywords は必須です")
                request_kwargs = {
                    "partner_tag": credentials["partner_tag"],
                    "keywords": str(search["keywords"]),
                    "search_index": search.get("search_index", "Computers"),
                    "item_count": int_value(search.get("item_count")) or 10,
                    "item_page": int_value(search.get("item_page")) or 1,
                    "min_saving_percent": int_value(search.get("min_saving_percent")),
                    "min_price": int_value(search.get("min_price")),
                    "max_price": int_value(search.get("max_price")),
                    "resources": OFFER_RESOURCES,
                    "currency_of_preference": "JPY",
                    "languages_of_preference": ["ja_JP"],
                }
                request = SearchItemsRequestContent(
                    **{key: value for key, value in request_kwargs.items() if value is not None}
                )
                response = api.search_items(
                    x_marketplace=marketplace,
                    search_items_request_content=request,
                    _request_timeout=request_timeout,
                )
                data = response.to_dict()
                container = dict_get(data, "searchResult", "SearchResult") or {}
                items = dict_get(container, "items", "Items") or []
                search_id = str(search.get("id") or f"search-{search_index + 1}")
                context = {
                    "operation": operation,
                    "id": search_id,
                    "keywords": search["keywords"],
                    "total_result_count": dict_get(container, "totalResultCount", "TotalResultCount"),
                    "search_url": dict_get(container, "searchURL", "SearchURL"),
                }
                for item in items:
                    asin = display_value(dict_get(item, "asin", "ASIN"))
                    if asin:
                        discoveries.setdefault(asin.upper(), []).append(search_id)

            for item in items:
                asin = display_value(dict_get(item, "asin", "ASIN"))
                if asin:
                    item_by_asin[asin.upper()] = item
            source_calls.append({**context, "status": "ok", "returned_count": len(items)})
            for error in dict_get(data, "errors", "Errors") or []:
                errors.append(error_record("creators_api_response", error, context=context))
        except Exception as exc:  # The generated SDK exposes multiple auth/HTTP exception classes.
            context = (
                {"operation": operation, "asins": payload}
                if operation == "get_items"
                else {
                    "operation": operation,
                    "id": str(payload[1].get("id") or f"search-{payload[0] + 1}"),
                    "keywords": payload[1].get("keywords"),
                }
            )
            errors.append(error_record("creators_api_call", exc, context=context))
            source_calls.append({**context, "status": "error", "returned_count": 0})
        if call_index + 1 < len(calls) and request_delay > 0:
            time.sleep(request_delay)

    return {
        "itemsResult": {"items": list(item_by_asin.values())},
        "errors": errors,
        "discoveries": discoveries,
        "sourceCalls": source_calls,
    }


def normalize_offer(listing: dict[str, Any]) -> dict[str, Any]:
    price = dict_get(listing, "price", "Price") or {}
    money = dict_get(price, "money", "Money") or {}
    basis = dict_get(price, "savingBasis", "SavingBasis") or {}
    basis_money = dict_get(basis, "money", "Money") or {}
    savings = dict_get(price, "savings", "Savings") or {}
    savings_money = dict_get(savings, "money", "Money") or {}
    points = dict_get(listing, "loyaltyPoints", "LoyaltyPoints") or {}
    merchant = dict_get(listing, "merchantInfo", "MerchantInfo") or {}
    availability = dict_get(listing, "availability", "Availability") or {}
    condition = dict_get(listing, "condition", "Condition") or {}
    deal = dict_get(listing, "dealDetails", "DealDetails") or {}
    deal_access = display_value(dict_get(deal, "accessType", "AccessType"))
    return {
        "price_yen": int_value(dict_get(money, "amount", "Amount")),
        "display_price": display_value(dict_get(money, "displayAmount", "DisplayAmount")),
        "currency": display_value(dict_get(money, "currency", "Currency")),
        "saving_basis_yen": int_value(dict_get(basis_money, "amount", "Amount")),
        "saving_basis_type": display_value(dict_get(basis, "savingBasisType", "SavingBasisType")),
        "savings_yen": int_value(dict_get(savings_money, "amount", "Amount")),
        "savings_percent": int_value(dict_get(savings, "percentage", "Percentage")),
        "points_yen": int_value(dict_get(points, "points", "Points")) or 0,
        "seller": {
            "name": display_value(dict_get(merchant, "name", "Name")),
            "id": display_value(dict_get(merchant, "id", "Id", "ID")),
        },
        "shipper": None,
        "availability": {
            "type": display_value(dict_get(availability, "type", "Type")),
            "message": display_value(dict_get(availability, "message", "Message")),
            "max_order_quantity": int_value(
                dict_get(availability, "maxOrderQuantity", "MaxOrderQuantity")
            ),
        },
        "condition": {
            "value": display_value(dict_get(condition, "value", "Value")),
            "subcondition": display_value(dict_get(condition, "subCondition", "SubCondition")),
            "note": display_value(dict_get(condition, "conditionNote", "ConditionNote")),
        },
        "deal": {
            "access_type": deal_access,
            "badge": display_value(dict_get(deal, "badge", "Badge")),
            "start_at": display_value(dict_get(deal, "startTime", "StartTime")),
            "end_at": display_value(dict_get(deal, "endTime", "EndTime")),
            "percent_claimed": int_value(dict_get(deal, "percentClaimed", "PercentClaimed")),
            "prime_exclusive": deal_access == "PRIME_EXCLUSIVE",
        },
        "offer_type": display_value(dict_get(listing, "type", "Type")),
        "is_buy_box_winner": dict_get(listing, "isBuyBoxWinner", "IsBuyBoxWinner"),
    }


def offer_rank(offer: dict[str, Any]) -> tuple[bool, int, bool, int]:
    availability = str((offer.get("availability") or {}).get("type") or "").upper()
    availability_score = 2 if availability in AVAILABLE_TYPES else (0 if availability in UNAVAILABLE_TYPES else 1)
    price = int_value(offer.get("price_yen"))
    return (
        bool(offer.get("is_buy_box_winner")),
        availability_score,
        price is not None,
        -price if price is not None else 0,
    )


def select_primary_offer(offers: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(offers, key=offer_rank) if offers else None


def normalize_confirmations(raw: Any) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    source = raw.get("items", raw) if isinstance(raw, dict) else raw
    if isinstance(source, dict):
        iterable = [
            {"asin": key, **value}
            for key, value in source.items()
            if isinstance(value, dict)
        ]
    elif isinstance(source, list):
        iterable = source
    else:
        raise ValueError("商品ページ確認JSONの items は配列またはASIN別オブジェクトにしてください")
    result: dict[str, dict[str, Any]] = {}
    for item in iterable:
        if not isinstance(item, dict) or not item.get("asin"):
            raise ValueError("商品ページ確認JSONの各項目には asin が必要です")
        asin = extract_asin(str(item["asin"]))
        result[asin] = {**item, "asin": asin}
    return result


def page_missing_fields(confirmation: dict[str, Any] | None) -> list[str]:
    if not confirmation:
        return PAGE_CHECK_FIELDS.copy()
    return [field for field in PAGE_CHECK_FIELDS if confirmation.get(field) is None]


def seller_is_amazon(value: Any) -> bool:
    return str(value or "").strip().lower() in AMAZON_SELLER_NAMES


def condition_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"new", "新品"}:
        return "new"
    if "refurb" in text or "整備済" in text:
        return "refurbished"
    if "used" in text or "中古" in text:
        return "used"
    if "openbox" in text or "open box" in text or "開封" in text:
        return "open_box"
    return text or "unknown"


def build_payment(
    offer: dict[str, Any] | None,
    confirmation: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    offer = offer or {}
    confirmation = confirmation or {}
    api_price = int_value(offer.get("price_yen"))
    api_points = int_value(offer.get("points_yen")) or 0
    page_price = int_value(confirmation.get("price_yen"))
    page_points = int_value(confirmation.get("points_yen"))
    prime_price = int_value(confirmation.get("prime_exclusive_price_yen"))

    base_price = page_price if page_price is not None else api_price
    adopted_source = "amazon_product_page" if page_price is not None else "amazon_creators_api"
    if prime_price is not None and confirmation.get("prime_eligible") is True:
        base_price = prime_price
        adopted_source = "amazon_product_page_prime_price"

    coupon_offer = int_value(confirmation.get("coupon_discount_yen"))
    coupon = coupon_offer or 0
    if confirmation.get("coupon_applied") is not True:
        coupon = 0
    register_offer = int_value(confirmation.get("register_discount_yen"))
    register_discount = register_offer or 0
    if confirmation.get("register_discount_applied") is not True:
        register_discount = 0
    shipping = int_value(confirmation.get("shipping_yen"))
    payment = int_value(confirmation.get("payment_amount_yen"))
    if payment is None and base_price is not None:
        # A confirmed product-page price must not silently assume free shipping.
        # The API-only fallback preserves the prior price-only behavior but is
        # explicitly left as an unverified candidate elsewhere in the result.
        if page_price is None or shipping is not None:
            payment = max(base_price - coupon - register_discount, 0) + (shipping or 0)
    adopted_points = page_points if page_points is not None else api_points
    effective = max(payment - adopted_points, 0) if payment is not None else None
    return (
        {
            "base_price_yen": base_price,
            "coupon_offer_yen": coupon_offer,
            "coupon_offer_percent": confirmation.get("coupon_discount_percent"),
            "coupon_applied": confirmation.get("coupon_applied"),
            "coupon_discount_yen": coupon,
            "register_discount_offer_yen": register_offer,
            "register_discount_offer_percent": confirmation.get("register_discount_percent"),
            "register_discount_applied": confirmation.get("register_discount_applied"),
            "register_discount_yen": register_discount,
            "shipping_yen": shipping,
            "shipping_checked": confirmation.get("shipping_checked"),
            "payment_amount_yen": payment,
            "points_yen": adopted_points,
            "effective_price_yen": effective,
            "coupon_checked": confirmation.get("coupon_checked"),
            "register_discount_checked": confirmation.get("register_discount_checked"),
            "prime_exclusive_price_yen": prime_price,
        },
        {
            "creators_api": {"price_yen": api_price, "points_yen": api_points},
            "amazon_product_page": {
                "confirmed_at": confirmation.get("confirmed_at"),
                "price_yen": page_price,
                "points_yen": page_points,
            },
            "adopted_source": adopted_source if base_price is not None else None,
            "price_difference_page_minus_api_yen": (
                page_price - api_price if page_price is not None and api_price is not None else None
            ),
            "points_difference_page_minus_api": (
                page_points - api_points if page_points is not None else None
            ),
        },
    )


def build_product(
    item: dict[str, Any],
    *,
    fetched_at: str,
    confirmation: dict[str, Any] | None,
    discovery_sources: list[str],
) -> dict[str, Any]:
    asin = (display_value(dict_get(item, "asin", "ASIN")) or "").upper()
    info = dict_get(item, "itemInfo", "ItemInfo") or {}
    manufacture = dict_get(info, "manufactureInfo", "ManufactureInfo") or {}
    byline = dict_get(info, "byLineInfo", "ByLineInfo") or {}
    raw_listings = dict_get(dict_get(item, "offersV2", "OffersV2") or {}, "listings", "Listings") or []
    offers = [normalize_offer(value) for value in raw_listings if isinstance(value, dict)]
    primary = select_primary_offer(offers)
    payment, comparison = build_payment(primary, confirmation)
    confirmation = confirmation or {}

    api_seller = ((primary or {}).get("seller") or {}).get("name")
    seller = confirmation.get("seller") if confirmation.get("seller") is not None else api_seller
    api_availability = (primary or {}).get("availability") or {}
    api_condition = (primary or {}).get("condition") or {}
    condition = confirmation.get("condition") or api_condition.get("value")
    model = confirmation.get("model") or display_value(dict_get(manufacture, "model", "Model"))
    warranty = confirmation.get("warranty") or display_value(dict_get(manufacture, "warranty", "Warranty"))

    exclusion_reasons: list[str] = []
    page_availability = confirmation.get("availability")
    if confirmation.get("cartable") is False:
        exclusion_reasons.append("公式商品ページでカート投入不可")
    if any(text in str(page_availability or "") for text in OUT_OF_STOCK_TEXT):
        exclusion_reasons.append("公式商品ページで在庫切れまたは販売終了")
    if page_availability is None and str(api_availability.get("type") or "").upper() in UNAVAILABLE_TYPES:
        exclusion_reasons.append("Creators APIで在庫なし")
    difference = comparison.get("price_difference_page_minus_api_yen")
    if isinstance(difference, int) and difference > 0:
        exclusion_reasons.append("公式商品ページでAPI取得価格より値上がり")
    if confirmation.get("seller_trusted") is False:
        exclusion_reasons.append("販売元の信頼性確認で不適格")
    if confirmation.get("model_matches") is False:
        exclusion_reasons.append("型番不一致")
    if confirmation.get("condition_matches") is False:
        exclusion_reasons.append("商品状態不一致")

    seller_trusted = confirmation.get("seller_trusted")
    if seller_trusted is None and seller_is_amazon(seller):
        seller_trusted = True
    confirmation_for_check = {**confirmation, "seller_trusted": seller_trusted}
    missing = page_missing_fields(confirmation_for_check or None)
    verification_status = "excluded" if exclusion_reasons else (
        "product_page_confirmed" if not missing else "amazon_verification_candidate"
    )
    product_url = confirmation.get("product_url") or display_value(
        dict_get(item, "detailPageURL", "DetailPageURL")
    ) or f"https://www.amazon.co.jp/dp/{asin}"

    return {
        "asin": asin,
        "record_status": "ok",
        "product_name": display_value(dict_get(info, "title", "Title")),
        "brand": display_value(dict_get(byline, "brand", "Brand")),
        "model": model,
        "part_number": display_value(dict_get(manufacture, "itemPartNumber", "ItemPartNumber")),
        "warranty": warranty,
        "product_url": product_url,
        "observed_at": confirmation.get("confirmed_at") or fetched_at,
        "discovery_sources": discovery_sources,
        "payment": payment,
        "seller": seller,
        "seller_id": ((primary or {}).get("seller") or {}).get("id"),
        "seller_trusted": seller_trusted,
        "shipper": confirmation.get("shipper"),
        "availability": {
            "page_text": page_availability,
            "api_type": api_availability.get("type"),
            "api_message": api_availability.get("message"),
            "cartable": confirmation.get("cartable"),
        },
        "condition": condition,
        "condition_detail": api_condition,
        "deal": (primary or {}).get("deal"),
        "offer_type": (primary or {}).get("offer_type"),
        "is_buy_box_winner": (primary or {}).get("is_buy_box_winner"),
        "offer_count": len(offers),
        "all_offers": offers,
        "source_comparison": comparison,
        "verification": {
            "status": verification_status,
            "product_page_confirmed_at": confirmation.get("confirmed_at"),
            "missing_product_page_fields": missing,
            "purchase_before_check": bool(missing),
            "exclusion_reasons": exclusion_reasons,
        },
    }


def load_history(path: Path | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if path is None or not path.exists():
        return [], []
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("object required")
            records.append(value)
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(error_record("history_read", exc, context={"line": line_number}))
    return records, errors


def add_year_low(product: dict[str, Any], history: list[dict[str, Any]]) -> None:
    observed_at = str(product.get("observed_at") or "")
    try:
        year = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00")).year
    except ValueError:
        year = dt.datetime.now().year
    normalized_condition = condition_key(product.get("condition"))
    relevant_prices = [
        int_value(row.get("effective_price_yen"))
        for row in history
        if row.get("asin") == product.get("asin")
        and int_value(row.get("year")) == year
        and str(row.get("condition_key") or condition_key(row.get("condition")))
        == normalized_condition
        and int_value(row.get("effective_price_yen")) is not None
    ]
    prior_low = min(relevant_prices, default=None)
    effective = int_value((product.get("payment") or {}).get("effective_price_yen"))
    product["year_low_reference"] = {
        "year": year,
        "same_asin_condition_observations_before": len(relevant_prices),
        "lowest_effective_price_yen_before": prior_low,
        "is_new_year_low": effective is not None and prior_low is not None and effective < prior_low,
        "is_year_low_tie": effective is not None and prior_low is not None and effective == prior_low,
        "not_enough_history": prior_low is None,
    }


def append_history(path: Path, products: list[dict[str, Any]]) -> int:
    observations: list[dict[str, Any]] = []
    for product in products:
        payment = product.get("payment") or {}
        effective = int_value(payment.get("effective_price_yen"))
        if (
            product.get("record_status") != "ok"
            or effective is None
            or payment.get("reference_only") is True
            or (product.get("verification") or {}).get("status")
            != "product_page_confirmed"
        ):
            continue
        observations.append(
            {
                "observed_at": product.get("observed_at"),
                "year": (product.get("year_low_reference") or {}).get("year"),
                "asin": product.get("asin"),
                "condition": product.get("condition"),
                "condition_key": condition_key(product.get("condition")),
                "payment_amount_yen": int_value(payment.get("payment_amount_yen")),
                "points_yen": int_value(payment.get("points_yen")),
                "effective_price_yen": effective,
                "adopted_source": (product.get("source_comparison") or {}).get("adopted_source"),
                "verification_status": (product.get("verification") or {}).get("status"),
            }
        )
    if not observations:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        for observation in observations:
            file.write(json.dumps(observation, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(observations)


def build_result(
    raw: dict[str, Any],
    *,
    requested_asins: list[str],
    search_count: int,
    marketplace: str,
    confirmations: dict[str, dict[str, Any]],
    history: list[dict[str, Any]],
    fetched_at: str | None = None,
) -> dict[str, Any]:
    fetched_at = fetched_at or now_iso()
    container = dict_get(raw, "itemsResult", "ItemsResult") or {}
    items = dict_get(container, "items", "Items") or []
    discoveries = dict_get(raw, "discoveries", "Discoveries") or {}
    products: list[dict[str, Any]] = []
    returned: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        asin = (display_value(dict_get(item, "asin", "ASIN")) or "").upper()
        if asin:
            returned.add(asin)
        product = build_product(
            item,
            fetched_at=fetched_at,
            confirmation=confirmations.get(asin),
            discovery_sources=list(discoveries.get(asin) or []),
        )
        add_year_low(product, history)
        products.append(product)

    errors = list(dict_get(raw, "errors", "Errors") or [])
    missing = [asin for asin in requested_asins if asin not in returned]
    for asin in missing:
        errors.append(
            {
                "stage": "result_validation",
                "type": "MissingItem",
                "message": "要求したASINがCreators APIレスポンスにありません",
                "context": {"asin": asin},
            }
        )
        products.append(
            {
                "asin": asin,
                "record_status": "error",
                "product_url": f"https://www.amazon.co.jp/dp/{asin}",
                "observed_at": fetched_at,
                "verification": {
                    "status": "acquisition_failed",
                    "missing_product_page_fields": PAGE_CHECK_FIELDS.copy(),
                    "purchase_before_check": True,
                    "exclusion_reasons": [],
                },
            }
        )

    acquired = sum(product.get("record_status") == "ok" for product in products)
    source_status = "error" if acquired == 0 and errors else ("partial" if errors else "ok")
    required_source_failure = bool(errors)
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "Amazon Creators API / Amazon.co.jp product-page confirmations",
        "source_status": source_status,
        "required_source_failure": required_source_failure,
        "monitor_status_hint": "監視エラー" if required_source_failure else None,
        "marketplace": marketplace,
        "fetched_at": fetched_at,
        "requested_asin_count": len(requested_asins),
        "search_count": search_count,
        "acquired_count": acquired,
        "error_count": len(errors),
        "errors": errors,
        "source_calls": dict_get(raw, "sourceCalls", "SourceCalls") or [],
        "limitations": [
            "Creators APIは商品ページクーポン、発送元、カート投入可否を返さない",
            "商品ページ確認JSONがない項目は未確認であり、購入前にAmazon公式ページ確認が必要",
            "年最安値はこのツールが同一ASIN・同一状態で保存した履歴内だけの参考判定",
        ],
        "assistant_handoff": {
            "is_final_notification_decision": False,
            "instruction": (
                "このJSONはAmazon確認の補助入力です。他店比較後に通知判定してください。"
                " source_status=errorまたはrequired_source_failure=trueなら、"
                "空応答にせず監視エラーとして失敗範囲を記載してください。"
            ),
        },
        "products": products,
    }


def chimolog_item_to_api_shape(item: dict[str, Any]) -> dict[str, Any]:
    """Reuse offer normalization while keeping the public JSON reference-only."""
    price = int_value(item.get("price_yen"))
    points = int_value(item.get("points_total")) or 0
    reference = int_value(item.get("reference_high_yen"))
    discount = int_value(item.get("discount_amount_yen"))
    listing: dict[str, Any] = {
        "loyaltyPoints": {"points": points},
        "price": {
            "money": {
                "amount": price,
                "currency": "JPY",
                "displayAmount": f"￥{price:,}" if price is not None else None,
            },
            "savingBasis": {
                "money": {"amount": reference, "currency": "JPY"},
            },
            "savings": {
                "money": {"amount": discount, "currency": "JPY"},
                "percentage": item.get("discount_rate_percent"),
            },
        },
        "type": "CHIMOLOG_PUBLIC_REFERENCE",
    }
    asin = str(item.get("asin") or "").upper()
    return {
        "asin": asin,
        "detailPageURL": f"https://www.amazon.co.jp/dp/{asin}",
        "itemInfo": {"title": {"displayValue": item.get("title")}},
        "offersV2": {"listings": [listing]},
    }


def build_chimolog_result(
    raw: dict[str, Any],
    *,
    requested_asins: list[str],
    marketplace: str,
    confirmations: dict[str, dict[str, Any]],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Turn public candidates into the common schema, requiring page confirmation."""
    source_items = raw.get("items") or []
    if not isinstance(source_items, list):
        raise ValueError("ちもろぐ入力JSONの items は配列である必要があります")
    wanted = set(requested_asins)
    selected = [
        item
        for item in source_items
        if isinstance(item, dict)
        and item.get("asin")
        and (not wanted or str(item["asin"]).upper() in wanted)
    ]
    by_asin = {str(item["asin"]).upper(): item for item in selected}
    transformed = {
        "itemsResult": {
            "items": [chimolog_item_to_api_shape(item) for item in selected]
        },
        "errors": list(raw.get("errors") or []),
        "discoveries": {asin: ["chimolog_public_json"] for asin in by_asin},
        "sourceCalls": list(raw.get("source_calls") or []),
    }
    result = build_result(
        transformed,
        requested_asins=requested_asins,
        search_count=0,
        marketplace=marketplace,
        confirmations=confirmations,
        history=history,
        fetched_at=raw.get("fetched_at") or now_iso(),
    )
    for product in result["products"]:
        source_item = by_asin.get(str(product.get("asin") or "").upper())
        if not source_item or product.get("record_status") != "ok":
            continue
        product.update(
            {
                "category": source_item.get("category"),
                "themes": source_item.get("themes") or [],
                "theme_labels": source_item.get("theme_labels") or [],
                "affiliate_url": source_item.get("affiliate_url"),
                "review_url": source_item.get("review_url"),
                "image_url": source_item.get("image_url"),
                "public_discount": {
                    "reference_high_yen": source_item.get("reference_high_yen"),
                    "discount_amount_yen": source_item.get("discount_amount_yen"),
                    "discount_rate_percent": source_item.get("discount_rate_percent"),
                    "is_discounted": source_item.get("is_discounted"),
                },
            }
        )
        comparison = product.get("source_comparison") or {}
        public_price = comparison.pop("creators_api", {})
        comparison["chimolog_public_json"] = {
            **public_price,
            "product_fetched_at": source_item.get("fetched_at"),
            "adopted_public_source": source_item.get("adopted_public_source"),
            "public_source_comparison": source_item.get("source_comparison"),
        }
        if comparison.get("adopted_source") == "amazon_creators_api":
            comparison["adopted_source"] = "chimolog_public_json_reference"
        reference_only = (
            comparison.get("adopted_source") == "chimolog_public_json_reference"
        )
        (product.get("payment") or {})["reference_only"] = reference_only
        (product.get("year_low_reference") or {})[
            "candidate_price_only"
        ] = reference_only

    if raw.get("source_status") == "error":
        result["source_status"] = "error"
    elif raw.get("source_status") == "partial":
        result["source_status"] = "partial"
    result.update(
        {
            "source": raw.get("source")
            or "Chimolog public JSON / Amazon.co.jp confirmations",
            "required_source_failure": raw.get("required_source_failure") is True,
            "monitor_status_hint": raw.get("monitor_status_hint"),
            "public_source_metadata": raw.get("metadata") or {},
            "limitations": list(raw.get("limitations") or [])
            + [
                "Amazon商品ページ確認値がない支払額・実質価格は参考値であり通知確定に使わない",
            ],
        }
    )
    return result


def confirmation_item_to_api_shape(confirmation: dict[str, Any]) -> dict[str, Any]:
    """Create the minimal common item shape without inventing API offer facts."""
    asin = extract_asin(str(confirmation.get("asin") or ""))
    return {
        "asin": asin,
        "detailPageURL": confirmation.get("product_url")
        or f"https://www.amazon.co.jp/dp/{asin}",
        "itemInfo": {
            "title": {"displayValue": confirmation.get("title")},
            "manufactureInfo": {
                "model": {"displayValue": confirmation.get("model")},
                "warranty": {"displayValue": confirmation.get("warranty")},
            },
        },
        "offersV2": {"listings": []},
    }


def build_confirmation_result(
    source_metadata: dict[str, Any],
    *,
    requested_asins: list[str],
    marketplace: str,
    confirmations: dict[str, dict[str, Any]],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the common result from Amazon product pages, without any API."""
    selected_asins = requested_asins or list(confirmations)
    failed_statuses = {"captcha", "blocked", "acquisition_failed"}
    successful: dict[str, dict[str, Any]] = {}
    errors = list(source_metadata.get("errors") or [])
    failed_products: list[dict[str, Any]] = []

    for asin in selected_asins:
        confirmation = confirmations.get(asin)
        if confirmation is None:
            message = "Amazon公式商品ページ確認JSONに要求ASINがありません"
            errors.append(
                {
                    "stage": "amazon_product_page",
                    "type": "MissingConfirmation",
                    "message": message,
                    "context": {"asin": asin},
                }
            )
            failed_products.append(
                {
                    "asin": asin,
                    "record_status": "error",
                    "product_url": f"https://www.amazon.co.jp/dp/{asin}",
                    "observed_at": now_iso(),
                    "verification": {
                        "status": "acquisition_failed",
                        "page_acquisition_status": "missing",
                        "missing_product_page_fields": PAGE_CHECK_FIELDS.copy(),
                        "purchase_before_check": True,
                        "exclusion_reasons": [],
                        "error": message,
                    },
                }
            )
            continue
        acquisition_status = str(confirmation.get("acquisition_status") or "ok")
        if acquisition_status in failed_statuses:
            message = str(confirmation.get("error") or "Amazon公式商品ページ確認に失敗しました")
            # Browser output already contains the same error at the root. Keep a
            # single ASIN-level entry when possible.
            if not any(
                isinstance(error, dict)
                and (error.get("asin") == asin or (error.get("context") or {}).get("asin") == asin)
                for error in errors
            ):
                errors.append(
                    {
                        "stage": "amazon_product_page",
                        "type": "PageAcquisitionError",
                        "message": message,
                        "context": {"asin": asin},
                    }
                )
            failed_products.append(
                {
                    "asin": asin,
                    "record_status": "error",
                    "product_url": confirmation.get("product_url")
                    or f"https://www.amazon.co.jp/dp/{asin}",
                    "observed_at": confirmation.get("confirmed_at") or now_iso(),
                    "verification": {
                        "status": "acquisition_failed",
                        "page_acquisition_status": acquisition_status,
                        "missing_product_page_fields": PAGE_CHECK_FIELDS.copy(),
                        "purchase_before_check": True,
                        "exclusion_reasons": [],
                        "error": message,
                    },
                }
            )
            continue
        successful[asin] = confirmation

    transformed = {
        "itemsResult": {
            "items": [confirmation_item_to_api_shape(value) for value in successful.values()]
        },
        "errors": [],
        "discoveries": {asin: ["amazon_product_page"] for asin in successful},
    }
    result = build_result(
        transformed,
        requested_asins=[],
        search_count=0,
        marketplace=marketplace,
        confirmations=successful,
        history=history,
        fetched_at=source_metadata.get("finished_at") or now_iso(),
    )
    for product in result.get("products") or []:
        confirmation = successful.get(str(product.get("asin") or "")) or {}
        (product.get("verification") or {})["page_acquisition_status"] = (
            confirmation.get("acquisition_status") or "manual_confirmation"
        )
        comparison = product.get("source_comparison") or {}
        comparison.pop("creators_api", None)
        comparison.pop("price_difference_page_minus_api_yen", None)
        comparison.pop("points_difference_page_minus_api", None)
    result["products"].extend(failed_products)
    acquired = sum(product.get("record_status") == "ok" for product in result["products"])
    source_status = "ok" if not errors else ("partial" if acquired else "error")
    result.update(
        {
            "source": "Amazon.co.jp official product-page confirmations (API-free)",
            "source_status": source_status,
            "fetched_at": source_metadata.get("finished_at") or result.get("fetched_at"),
            "requested_asin_count": len(selected_asins),
            "acquired_count": acquired,
            "error_count": len(errors),
            "errors": errors,
            "required_source_failure": (
                source_status == "error"
                or source_metadata.get("required_source_failure") is True
            ),
            "monitor_status_hint": (
                "監視エラー" if source_status == "error" else source_metadata.get("monitor_status_hint")
            ),
            "limitations": [
                value
                for value in (result.get("limitations") or [])
                if not str(value).startswith("Creators API")
            ]
            + [
                "Creators APIとちもろぐを使用せず、Amazon商品ページで確認できた範囲だけを採用",
            ],
        }
    )
    return result


def error_result(message: str, *, marketplace: str, asins: list[str], search_count: int) -> dict[str, Any]:
    return build_result(
        {
            "itemsResult": {"items": []},
            "errors": [
                {
                    "stage": "configuration_or_fetch",
                    "type": "CheckerError",
                    "message": message,
                }
            ],
        },
        requested_asins=asins,
        search_count=search_count,
        marketplace=marketplace,
        confirmations={},
        history=[],
    )


def attach_quality_catalog_to_result(
    result: dict[str, Any],
    catalog: dict[str, Any],
    *,
    profile_ids: list[str] | None = None,
    as_of: dt.date | None = None,
) -> None:
    """Attach reusable catalog decisions without changing observed offer facts."""
    for profile_id in profile_ids or []:
        quality_profile_by_id(catalog, profile_id)
    matched = 0
    eligible = 0
    for product in result.get("products") or []:
        if product.get("record_status") != "ok":
            continue
        evaluation = attach_catalog_evaluation(
            catalog,
            product,
            profile_ids=profile_ids,
            as_of=as_of,
        )
        product["quality_catalog"] = evaluation
        matched += evaluation.get("match_status") == "matched"
        eligible += evaluation.get("automation_eligible") is True
    result["quality_catalog"] = {
        "enabled": True,
        "schema_version": catalog.get("schema_version"),
        "catalog_updated_at": catalog.get("updated_at"),
        "selected_profile_ids": profile_ids or [],
        "matched_product_count": matched,
        "automation_eligible_count": eligible,
        "rule": (
            "未登録・曖昧一致・品質ゲート不合格の製品は、価格が安くても"
            "自動通知対象にしない"
        ),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Amazon公式商品ページや任意のCreators API結果を価格監視用JSONへ正規化します",
    )
    parser.add_argument("products", nargs="*", help="ASINまたはAmazon商品URL")
    parser.add_argument("--config", type=Path, help="ASINと検索条件を含むJSON")
    parser.add_argument("--response-file", type=Path, help="取得済みレスポンスを使うオフラインモード")
    parser.add_argument(
        "--chimolog-file",
        type=Path,
        help="chimolog_source.pyの出力を使うAPI不要モード",
    )
    parser.add_argument(
        "--confirmation-only",
        action="store_true",
        help="商品ページ確認JSONだけを使うAPI不要モード",
    )
    parser.add_argument("--confirmation-file", type=Path, help="Amazon公式商品ページの確認JSON")
    parser.add_argument("--history-file", type=Path, help="年内価格履歴JSONL（実行後に追記）")
    quality_group = parser.add_mutually_exclusive_group()
    quality_group.add_argument(
        "--quality-catalog",
        type=Path,
        default=DEFAULT_QUALITY_CATALOG,
        help=(
            "製品探索と監視で共有する品質カタログJSON"
            f"（既定: {DEFAULT_QUALITY_CATALOG}）"
        ),
    )
    quality_group.add_argument(
        "--no-quality-catalog",
        action="store_true",
        help="品質カタログの照合を明示的に無効化する",
    )
    parser.add_argument(
        "--quality-profile",
        action="append",
        default=[],
        help="適用する用途プロファイルID（複数指定可、省略時は同カテゴリの全プロファイル）",
    )
    parser.add_argument("--quality-as-of", help="品質評価の基準日 YYYY-MM-DD")
    parser.add_argument("--no-history-write", action="store_true")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--request-delay", type=float, default=1.1)
    parser.add_argument("--indent", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path)
    return parser


def write_result(result: dict[str, Any], *, output: Path | None, indent: int) -> None:
    text = json.dumps(result, ensure_ascii=False, indent=indent) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config: dict[str, Any] = {"asins": [], "searches": [], "marketplace": DEFAULT_MARKETPLACE}
    try:
        config = load_config(args.config, args.products)
        confirmation_source = load_json(args.confirmation_file) if args.confirmation_file else {}
        confirmations = normalize_confirmations(confirmation_source) if args.confirmation_file else {}
        selected_modes = sum(
            bool(value) for value in (args.response_file, args.chimolog_file, args.confirmation_only)
        )
        if selected_modes > 1:
            raise ValueError(
                "--response-file、--chimolog-file、--confirmation-onlyは同時指定できません"
            )
        input_mode = "creators_api"
        if args.confirmation_only:
            if not args.confirmation_file:
                raise ValueError("--confirmation-only には --confirmation-file が必要です")
            if not isinstance(confirmation_source, dict):
                raise ValueError("商品ページ確認JSONのルートはオブジェクトである必要があります")
            raw = confirmation_source
            input_mode = "confirmation_only"
            if not config["asins"]:
                config["asins"] = list(confirmations)
        elif args.chimolog_file:
            raw = load_json(args.chimolog_file)
            if not isinstance(raw, dict):
                raise ValueError("ちもろぐ入力JSONのルートはオブジェクトである必要があります")
            input_mode = "chimolog"
        elif args.response_file:
            raw = load_json(args.response_file)
            if not isinstance(raw, dict):
                raise ValueError("レスポンスJSONのルートはオブジェクトである必要があります")
            if not config["asins"]:
                container = dict_get(raw, "itemsResult", "ItemsResult") or {}
                items = dict_get(container, "items", "Items") or []
                config["asins"] = unique_asins(
                    str(dict_get(item, "asin", "ASIN"))
                    for item in items
                    if dict_get(item, "asin", "ASIN")
                )
        else:
            if not config["asins"] and not config["searches"]:
                raise CheckerError("ASIN、商品URL、または--configのsearchesを指定してください")
            raw = fetch_official_data(
                asins=config["asins"],
                searches=config["searches"],
                marketplace=config["marketplace"],
                request_timeout=args.timeout,
                request_delay=args.request_delay,
            )
        history, history_errors = load_history(args.history_file)
        if history_errors:
            raw.setdefault("errors", []).extend(history_errors)
        if input_mode == "confirmation_only":
            result = build_confirmation_result(
                raw,
                requested_asins=config["asins"],
                marketplace=config["marketplace"],
                confirmations=confirmations,
                history=history,
            )
        elif input_mode == "chimolog":
            result = build_chimolog_result(
                raw,
                requested_asins=config["asins"],
                marketplace=config["marketplace"],
                confirmations=confirmations,
                history=history,
            )
        else:
            result = build_result(
                raw,
                requested_asins=config["asins"],
                search_count=len(config["searches"]),
                marketplace=config["marketplace"],
                confirmations=confirmations,
                history=history,
            )
        if not args.no_quality_catalog:
            quality_as_of = None
            if args.quality_as_of:
                try:
                    quality_as_of = dt.date.fromisoformat(args.quality_as_of)
                except ValueError as exc:
                    raise ValueError("--quality-as-of はYYYY-MM-DD形式にしてください") from exc
            attach_quality_catalog_to_result(
                result,
                load_quality_catalog(args.quality_catalog),
                profile_ids=args.quality_profile,
                as_of=quality_as_of,
            )
            result["quality_catalog"]["catalog_path"] = str(args.quality_catalog)
            result["quality_catalog"]["loaded_by_default"] = (
                args.quality_catalog == DEFAULT_QUALITY_CATALOG
            )
        else:
            result["quality_catalog"] = {
                "enabled": False,
                "disabled_reason": "--no-quality-catalog",
                "rule": "品質カタログ照合は利用者が明示的に無効化",
            }
        if args.history_file and not args.no_history_write:
            result["history_appended_count"] = append_history(args.history_file, result["products"])
    except (CheckerError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        result = error_result(
            str(exc),
            marketplace=config["marketplace"],
            asins=config["asins"],
            search_count=len(config["searches"]),
        )

    write_result(result, output=args.output, indent=args.indent)
    return 1 if (
        result.get("source_status") == "error"
        or result.get("required_source_failure") is True
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())

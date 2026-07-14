#!/usr/bin/env python3
"""Reusable PC-parts quality catalog and product-discovery CLI.

The catalog deliberately contains no live prices.  Product identity, quality
facts and use-case profiles change at a different pace from offers, so price
collectors can attach their observations to this stable catalog without
turning temporary sale data into quality evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
DEFAULT_CATALOG = Path(__file__).with_name("catalog") / "quality_catalog.json"
SUPPORTED_OPERATORS = {
    "eq",
    "ne",
    "in",
    "not_in",
    "contains",
    "intersects",
    "gt",
    "gte",
    "lt",
    "lte",
    "exists",
}
QUALITY_STATUSES = {
    "research_required",
    "approved",
    "preferred",
    "rejected",
    "discontinued",
}
MISSING = object()


class CatalogError(ValueError):
    """A catalog or command cannot be evaluated safely."""


def load_catalog(path: Path, *, strict: bool = True) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CatalogError(f"品質カタログを読み込めません: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"品質カタログJSONが不正です: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CatalogError("品質カタログのルートはオブジェクトである必要があります")
    errors, _ = validate_catalog(raw)
    if strict and errors:
        raise CatalogError("品質カタログの検証に失敗: " + "; ".join(errors))
    return raw


def parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def validate_condition(condition: Any, location: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(condition, dict):
        return [f"{location} はオブジェクトである必要があります"]
    if not str(condition.get("path") or "").strip():
        errors.append(f"{location}.path は必須です")
    operation = str(condition.get("op") or "eq")
    if operation not in SUPPORTED_OPERATORS:
        errors.append(f"{location}.op={operation!r} は未対応です")
    if operation != "exists" and "value" not in condition:
        errors.append(f"{location}.value は必須です")
    if operation in {"in", "not_in", "intersects"} and not isinstance(
        condition.get("value"), list
    ):
        errors.append(f"{location}.value は配列である必要があります")
    return errors


def validate_catalog(catalog: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return validation errors and non-fatal maintenance warnings."""
    errors: list[str] = []
    warnings: list[str] = []
    if catalog.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version は {SCHEMA_VERSION} である必要があります"
        )
    settings = catalog.get("settings")
    if not isinstance(settings, dict):
        errors.append("settings はオブジェクトである必要があります")
        settings = {}
    tier_order = settings.get("quality_tier_order") or []
    if not isinstance(tier_order, list) or not all(
        isinstance(value, str) and value for value in tier_order
    ):
        errors.append("settings.quality_tier_order は文字列の配列である必要があります")
        tier_order = []

    profiles = catalog.get("profiles")
    if not isinstance(profiles, list):
        errors.append("profiles は配列である必要があります")
        profiles = []
    profile_ids: set[str] = set()
    for index, profile in enumerate(profiles):
        location = f"profiles[{index}]"
        if not isinstance(profile, dict):
            errors.append(f"{location} はオブジェクトである必要があります")
            continue
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id:
            errors.append(f"{location}.id は必須です")
        elif profile_id in profile_ids:
            errors.append(f"重複したプロファイルID: {profile_id}")
        profile_ids.add(profile_id)
        if not str(profile.get("category") or "").strip():
            errors.append(f"{location}.category は必須です")
        for key in ("requirements", "preferences"):
            conditions = profile.get(key) or []
            if not isinstance(conditions, list):
                errors.append(f"{location}.{key} は配列である必要があります")
                continue
            for condition_index, condition in enumerate(conditions):
                errors.extend(
                    validate_condition(
                        condition, f"{location}.{key}[{condition_index}]"
                    )
                )
                if key == "preferences" and isinstance(condition, dict):
                    weight = condition.get("weight", 0)
                    if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight < 0:
                        errors.append(
                            f"{location}.{key}[{condition_index}].weight は0以上の数値にしてください"
                        )

    products = catalog.get("products")
    if not isinstance(products, list):
        errors.append("products は配列である必要があります")
        products = []
    product_ids: set[str] = set()
    identifier_owners: dict[tuple[str, str], str] = {}
    for index, product in enumerate(products):
        location = f"products[{index}]"
        if not isinstance(product, dict):
            errors.append(f"{location} はオブジェクトである必要があります")
            continue
        product_id = str(product.get("id") or "").strip()
        if not product_id:
            errors.append(f"{location}.id は必須です")
        elif product_id in product_ids:
            errors.append(f"重複した製品ID: {product_id}")
        product_ids.add(product_id)
        for key in ("category", "brand", "model"):
            if not str(product.get(key) or "").strip():
                errors.append(f"{location}.{key} は必須です")
        identifiers = product.get("identifiers") or {}
        if not isinstance(identifiers, dict):
            errors.append(f"{location}.identifiers はオブジェクトである必要があります")
        else:
            for key, values in identifiers.items():
                if not isinstance(values, list) or not all(
                    isinstance(value, str) and value.strip() for value in values
                ):
                    errors.append(f"{location}.identifiers.{key} は文字列の配列にしてください")
                    continue
                for identifier in values:
                    normalized = normalize_identity(identifier)
                    owner = identifier_owners.get((key, normalized))
                    if owner and owner != product_id:
                        errors.append(
                            f"識別子が重複しています: {key}={identifier!r} ({owner}, {product_id})"
                        )
                    identifier_owners[(key, normalized)] = product_id
        if not isinstance(product.get("specs"), dict):
            errors.append(f"{location}.specs はオブジェクトである必要があります")
        quality = product.get("quality")
        if not isinstance(quality, dict):
            errors.append(f"{location}.quality はオブジェクトである必要があります")
            continue
        for key in ("status", "tier"):
            if not str(quality.get(key) or "").strip():
                errors.append(f"{location}.quality.{key} は必須です")
        if quality.get("status") not in QUALITY_STATUSES:
            errors.append(
                f"{location}.quality.status={quality.get('status')!r} は未対応です"
            )
        if tier_order and quality.get("tier") not in tier_order:
            errors.append(
                f"{location}.quality.tier={quality.get('tier')!r} は quality_tier_order にありません"
            )
        for key in ("strengths", "weaknesses", "risk_flags", "evidence"):
            if not isinstance(quality.get(key, []), list):
                errors.append(f"{location}.quality.{key} は配列である必要があります")
        reviewed_at = quality.get("reviewed_at")
        if reviewed_at and parse_date(reviewed_at) is None:
            errors.append(f"{location}.quality.reviewed_at はYYYY-MM-DD形式にしてください")
        if quality.get("status") in {"approved", "preferred"} and not quality.get("evidence"):
            warnings.append(f"{product_id}: 承認済みですが根拠資料がありません")
        for evidence_index, evidence in enumerate(quality.get("evidence") or []):
            evidence_location = f"{location}.quality.evidence[{evidence_index}]"
            if not isinstance(evidence, dict):
                errors.append(f"{evidence_location} はオブジェクトである必要があります")
                continue
            for key in ("kind", "title", "url", "checked_at"):
                if not str(evidence.get(key) or "").strip():
                    errors.append(f"{evidence_location}.{key} は必須です")
            if evidence.get("checked_at") and parse_date(evidence.get("checked_at")) is None:
                errors.append(f"{evidence_location}.checked_at はYYYY-MM-DD形式にしてください")

    return errors, warnings


def path_get(value: Any, path: str, default: Any = MISSING) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def comparable(value: Any) -> Any:
    return value.casefold().strip() if isinstance(value, str) else value


def condition_matches(actual: Any, operation: str, expected: Any = None) -> bool:
    exists = actual is not MISSING and actual is not None
    if operation == "exists":
        return exists is bool(expected)
    if not exists:
        return False
    normalized_actual = comparable(actual)
    normalized_expected = comparable(expected)
    if operation == "eq":
        return normalized_actual == normalized_expected
    if operation == "ne":
        return normalized_actual != normalized_expected
    if operation in {"in", "not_in"}:
        values = [comparable(value) for value in expected]
        result = normalized_actual in values
        return not result if operation == "not_in" else result
    if operation == "contains":
        if isinstance(actual, str):
            return str(expected).casefold() in actual.casefold()
        if isinstance(actual, (list, tuple, set)):
            return comparable(expected) in [comparable(value) for value in actual]
        return False
    if operation == "intersects":
        if not isinstance(actual, (list, tuple, set)):
            return False
        return bool(
            {comparable(value) for value in actual}
            & {comparable(value) for value in expected}
        )
    try:
        if operation == "gt":
            return actual > expected
        if operation == "gte":
            return actual >= expected
        if operation == "lt":
            return actual < expected
        if operation == "lte":
            return actual <= expected
    except TypeError:
        return False
    raise CatalogError(f"未対応の比較演算子: {operation}")


def evaluate_rule(product: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
    actual = path_get(product, str(rule["path"]))
    operation = str(rule.get("op") or "eq")
    expected = rule.get("value")
    matched = condition_matches(actual, operation, expected)
    return {
        "path": rule["path"],
        "label": rule.get("label") or rule["path"],
        "op": operation,
        "expected": expected,
        "actual": None if actual is MISSING else actual,
        "matched": matched,
    }


def merge_gate(catalog: dict[str, Any], profile: dict[str, Any] | None) -> dict[str, Any]:
    settings = catalog.get("settings") or {}
    gate = dict(settings.get("default_quality_gate") or {})
    if profile:
        overrides = profile.get("quality_gate") or {}
        for key, value in overrides.items():
            if key in {"forbidden_risk_flags", "required_evidence_kinds"}:
                gate[key] = sorted(set(gate.get(key) or []) | set(value or []))
            else:
                gate[key] = value
    return gate


def evaluate_quality_gate(
    catalog: dict[str, Any],
    product: dict[str, Any],
    profile: dict[str, Any] | None = None,
    *,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    as_of = as_of or dt.date.today()
    gate = merge_gate(catalog, profile)
    quality = product.get("quality") or {}
    failures: list[str] = []
    warnings: list[str] = []
    status = quality.get("status")
    allowed_statuses = gate.get("allowed_statuses") or []
    if allowed_statuses and status not in allowed_statuses:
        failures.append(f"品質状態 {status!r} は承認対象外")

    tier_order = (catalog.get("settings") or {}).get("quality_tier_order") or []
    tier = quality.get("tier")
    minimum_tier = gate.get("minimum_tier")
    if minimum_tier and tier_order:
        if tier not in tier_order or minimum_tier not in tier_order:
            failures.append("品質Tierを比較できない")
        elif tier_order.index(tier) < tier_order.index(minimum_tier):
            failures.append(f"品質Tier {tier} は最低条件 {minimum_tier} 未満")

    evidence = quality.get("evidence") or []
    minimum_evidence_count = int(gate.get("minimum_evidence_count") or 0)
    if len(evidence) < minimum_evidence_count:
        failures.append(
            f"根拠資料が不足（{len(evidence)}/{minimum_evidence_count}件）"
        )
    required_kinds = set(gate.get("required_evidence_kinds") or [])
    actual_kinds = {
        str(item.get("kind")) for item in evidence if isinstance(item, dict) and item.get("kind")
    }
    if required_kinds and not required_kinds.issubset(actual_kinds):
        missing = ", ".join(sorted(required_kinds - actual_kinds))
        failures.append(f"必要な根拠種別が不足: {missing}")

    forbidden = set(gate.get("forbidden_risk_flags") or [])
    risks = set(quality.get("risk_flags") or [])
    blocked_risks = sorted(forbidden & risks)
    if blocked_risks:
        failures.append("禁止リスク: " + ", ".join(blocked_risks))

    reviewed_at = parse_date(quality.get("reviewed_at"))
    max_age = gate.get("max_review_age_days")
    if max_age is not None:
        if reviewed_at is None:
            failures.append("品質確認日が未登録")
        else:
            age = (as_of - reviewed_at).days
            if age < 0:
                warnings.append("品質確認日が未来日")
            elif age > int(max_age):
                failures.append(f"品質確認が古い（{age}日経過）")

    return {
        "passed": not failures,
        "failures": failures,
        "warnings": warnings,
        "status": status,
        "tier": tier,
        "reviewed_at": quality.get("reviewed_at"),
        "evidence_count": len(evidence),
        "risk_flags": sorted(risks),
    }


def profile_by_id(catalog: dict[str, Any], profile_id: str) -> dict[str, Any]:
    for profile in catalog.get("profiles") or []:
        if profile.get("id") == profile_id:
            return profile
    raise CatalogError(f"用途プロファイルが見つかりません: {profile_id}")


def product_by_id(catalog: dict[str, Any], product_id: str) -> dict[str, Any]:
    for product in catalog.get("products") or []:
        if product.get("id") == product_id:
            return product
    raise CatalogError(f"製品が見つかりません: {product_id}")


def evaluate_product(
    catalog: dict[str, Any],
    product: dict[str, Any],
    profile: dict[str, Any] | None = None,
    *,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    requirements: list[dict[str, Any]] = []
    preferences: list[dict[str, Any]] = []
    if profile:
        requirements = [evaluate_rule(product, rule) for rule in profile.get("requirements") or []]
        for rule in profile.get("preferences") or []:
            result = evaluate_rule(product, rule)
            result["weight"] = float(rule.get("weight") or 0)
            preferences.append(result)
    category_matches = not profile or product.get("category") == profile.get("category")
    requirements_passed = category_matches and all(result["matched"] for result in requirements)
    total_weight = sum(result["weight"] for result in preferences)
    matched_weight = sum(result["weight"] for result in preferences if result["matched"])
    score = round((matched_weight / total_weight) * 100, 1) if total_weight else (
        100.0 if requirements_passed else 0.0
    )
    gate = evaluate_quality_gate(catalog, product, profile, as_of=as_of)
    return {
        "product_id": product.get("id"),
        "profile_id": profile.get("id") if profile else None,
        "category_matches": category_matches,
        "requirements_passed": requirements_passed,
        "quality_gate": gate,
        "automation_eligible": requirements_passed and gate["passed"],
        "preference_score": score,
        "matched_preference_weight": matched_weight,
        "total_preference_weight": total_weight,
        "requirements": requirements,
        "preferences": preferences,
    }


def flatten_text(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from flatten_text(child)
    elif isinstance(value, list):
        for child in value:
            yield from flatten_text(child)
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        yield str(value)


def parse_where(value: str) -> dict[str, Any]:
    match = re.fullmatch(r"([^:]+):(eq|ne|in|not_in|contains|intersects|gt|gte|lt|lte|exists):(.+)", value)
    if not match:
        raise CatalogError(
            "--where は path:op:value 形式です（例: specs.capacity_gb:gte:2000）"
        )
    path, operation, raw_value = match.groups()
    try:
        expected = json.loads(raw_value)
    except json.JSONDecodeError:
        expected = raw_value
    if operation in {"in", "not_in", "intersects"} and not isinstance(expected, list):
        expected = [part.strip() for part in raw_value.split(",") if part.strip()]
    if operation == "exists" and not isinstance(expected, bool):
        expected = raw_value.casefold() in {"1", "true", "yes"}
    return {"path": path, "op": operation, "value": expected, "label": value}


def find_products(
    catalog: dict[str, Any],
    *,
    profile: dict[str, Any] | None = None,
    category: str | None = None,
    query: str | None = None,
    where: list[dict[str, Any]] | None = None,
    include_ineligible: bool = False,
    as_of: dt.date | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    query_text = str(query or "").casefold().strip()
    for product in catalog.get("products") or []:
        if category and product.get("category") != category:
            continue
        if profile and product.get("category") != profile.get("category"):
            continue
        if query_text:
            haystack = "\n".join(flatten_text(product)).casefold()
            if any(token not in haystack for token in query_text.split()):
                continue
        extra_rules = [evaluate_rule(product, rule) for rule in where or []]
        if not all(result["matched"] for result in extra_rules):
            continue
        evaluation = evaluate_product(catalog, product, profile, as_of=as_of)
        if not include_ineligible and not evaluation["automation_eligible"]:
            continue
        results.append(
            {
                "product": product,
                "evaluation": evaluation,
                "where": extra_rules,
            }
        )
    tier_order = (catalog.get("settings") or {}).get("quality_tier_order") or []
    tier_rank = {tier: index for index, tier in enumerate(tier_order)}
    results.sort(
        key=lambda row: (
            bool(row["evaluation"]["automation_eligible"]),
            row["evaluation"]["preference_score"],
            tier_rank.get((row["product"].get("quality") or {}).get("tier"), -1),
            str(row["product"].get("brand") or "").casefold(),
            str(row["product"].get("model") or "").casefold(),
        ),
        reverse=True,
    )
    return results


def normalize_identity(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def match_products(
    catalog: dict[str, Any],
    *,
    asin: str | None = None,
    part_number: str | None = None,
    model: str | None = None,
    title: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Match an offer to catalog products without guessing short model names."""
    matches: list[dict[str, Any]] = []
    wanted_asin = normalize_identity(asin)
    wanted_part = normalize_identity(part_number)
    wanted_model = normalize_identity(model)
    normalized_title = normalize_identity(title)
    for product in catalog.get("products") or []:
        if category and product.get("category") != category:
            continue
        identifiers = product.get("identifiers") or {}
        reasons: list[str] = []
        confidence = 0
        asins = {normalize_identity(value) for value in identifiers.get("asins") or []}
        parts = {normalize_identity(value) for value in identifiers.get("part_numbers") or []}
        models = {
            normalize_identity(value)
            for value in [product.get("model"), *(product.get("aliases") or [])]
            if value
        }
        if wanted_asin and wanted_asin in asins:
            confidence = max(confidence, 100)
            reasons.append("asin")
        if wanted_part and wanted_part in parts:
            confidence = max(confidence, 95)
            reasons.append("part_number")
        if wanted_model and wanted_model in models:
            confidence = max(confidence, 90)
            reasons.append("model")
        title_models = [value for value in models if len(value) >= 6]
        if normalized_title and any(value in normalized_title for value in title_models):
            confidence = max(confidence, 70)
            reasons.append("title_model")
        if confidence:
            matches.append(
                {
                    "product": product,
                    "confidence": confidence,
                    "matched_by": sorted(set(reasons)),
                }
            )
    matches.sort(key=lambda item: (item["confidence"], item["product"]["id"]), reverse=True)
    return matches


def attach_catalog_evaluation(
    catalog: dict[str, Any],
    offer_product: dict[str, Any],
    *,
    profile_ids: list[str] | None = None,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    matches = match_products(
        catalog,
        asin=offer_product.get("asin"),
        part_number=offer_product.get("part_number"),
        model=offer_product.get("model"),
        title=offer_product.get("product_name"),
    )
    if not matches:
        return {
            "match_status": "not_cataloged",
            "automation_eligible": False,
            "purchase_before_check": True,
            "reasons": ["品質カタログに一致する製品がありません"],
        }
    best = matches[0]
    if len(matches) > 1 and matches[1]["confidence"] == best["confidence"]:
        return {
            "match_status": "ambiguous",
            "automation_eligible": False,
            "purchase_before_check": True,
            "candidates": [item["product"]["id"] for item in matches if item["confidence"] == best["confidence"]],
            "reasons": ["同じ確度で複数のカタログ製品に一致しました"],
        }
    product = best["product"]
    selected_profiles = (
        [profile_by_id(catalog, profile_id) for profile_id in profile_ids]
        if profile_ids
        else [
            profile
            for profile in catalog.get("profiles") or []
            if profile.get("category") == product.get("category")
        ]
    )
    evaluations = [
        evaluate_product(catalog, product, profile, as_of=as_of)
        for profile in selected_profiles
    ]
    base_gate = evaluate_quality_gate(catalog, product, as_of=as_of)
    eligible = base_gate["passed"] and (
        any(value["automation_eligible"] for value in evaluations)
        if evaluations
        else True
    )
    return {
        "match_status": "matched",
        "catalog_product_id": product.get("id"),
        "category": product.get("category"),
        "brand": product.get("brand"),
        "model": product.get("model"),
        "confidence": best["confidence"],
        "matched_by": best["matched_by"],
        "quality": product.get("quality"),
        "quality_gate": base_gate,
        "profile_evaluations": evaluations,
        "automation_eligible": eligible,
        "purchase_before_check": not eligible,
    }


def table(rows: list[list[Any]], headers: list[str]) -> str:
    values = [[str(value) for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in values:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    lines = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in values
    )
    return "\n".join(lines)


def output_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def parse_as_of(value: str | None) -> dt.date | None:
    if not value:
        return None
    parsed = parse_date(value)
    if parsed is None:
        raise CatalogError("--as-of はYYYY-MM-DD形式にしてください")
    return parsed


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PCパーツ品質カタログを検証・検索・自動判定します",
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="カタログを検証")
    validate_parser.add_argument("--format", choices=("json", "text"), default="text")

    profiles_parser = subparsers.add_parser("profiles", help="用途プロファイルを一覧表示")
    profiles_parser.add_argument("--category")
    profiles_parser.add_argument("--format", choices=("json", "table"), default="table")

    search_parser = subparsers.add_parser("search", help="用途や仕様から製品を検索・比較")
    search_parser.add_argument("--profile")
    search_parser.add_argument("--category")
    search_parser.add_argument("--query")
    search_parser.add_argument("--where", action="append", default=[])
    search_parser.add_argument("--include-ineligible", action="store_true")
    search_parser.add_argument("--as-of")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--format", choices=("json", "table"), default="table")

    evaluate_parser = subparsers.add_parser("evaluate", help="1製品を用途条件で詳細評価")
    evaluate_parser.add_argument("product_id")
    evaluate_parser.add_argument("--profile")
    evaluate_parser.add_argument("--as-of")

    match_parser = subparsers.add_parser("match", help="ASINや型番をカタログ製品へ照合")
    match_parser.add_argument("--asin")
    match_parser.add_argument("--part-number")
    match_parser.add_argument("--model")
    match_parser.add_argument("--title")
    match_parser.add_argument("--category")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        catalog = load_catalog(args.catalog, strict=False)
        errors, warnings = validate_catalog(catalog)
        if args.command == "validate":
            result = {
                "status": "ok" if not errors else "error",
                "catalog": str(args.catalog),
                "product_count": len(catalog.get("products") or []),
                "profile_count": len(catalog.get("profiles") or []),
                "errors": errors,
                "warnings": warnings,
            }
            if args.format == "json":
                output_json(result)
            else:
                print(
                    f"{result['status']}: products={result['product_count']} "
                    f"profiles={result['profile_count']} errors={len(errors)} warnings={len(warnings)}"
                )
                for message in errors:
                    print(f"ERROR: {message}")
                for message in warnings:
                    print(f"WARNING: {message}")
            return 1 if errors else 0
        if errors:
            raise CatalogError("品質カタログの検証に失敗: " + "; ".join(errors))

        if args.command == "profiles":
            profiles = [
                profile for profile in catalog.get("profiles") or []
                if not args.category or profile.get("category") == args.category
            ]
            if args.format == "json":
                output_json({"count": len(profiles), "profiles": profiles})
            else:
                print(
                    table(
                        [
                            [profile["id"], profile["category"], profile.get("name", ""), profile.get("description", "")]
                            for profile in profiles
                        ],
                        ["ID", "CATEGORY", "NAME", "DESCRIPTION"],
                    )
                )
            return 0

        if args.command == "search":
            profile = profile_by_id(catalog, args.profile) if args.profile else None
            results = find_products(
                catalog,
                profile=profile,
                category=args.category,
                query=args.query,
                where=[parse_where(value) for value in args.where],
                include_ineligible=args.include_ineligible,
                as_of=parse_as_of(args.as_of),
            )[: max(args.limit, 0)]
            if args.format == "json":
                output_json(
                    {
                        "count": len(results),
                        "profile_id": args.profile,
                        "results": results,
                    }
                )
            else:
                print(
                    table(
                        [
                            [
                                "yes" if row["evaluation"]["automation_eligible"] else "no",
                                row["evaluation"]["preference_score"],
                                row["product"]["id"],
                                row["product"]["category"],
                                f"{row['product']['brand']} {row['product']['model']}",
                                (row["product"].get("quality") or {}).get("tier"),
                                "; ".join(row["evaluation"]["quality_gate"]["failures"]),
                            ]
                            for row in results
                        ],
                        ["ELIGIBLE", "SCORE", "ID", "CATEGORY", "PRODUCT", "TIER", "GATE FAILURES"],
                    )
                )
            return 0

        if args.command == "evaluate":
            product = product_by_id(catalog, args.product_id)
            profile = profile_by_id(catalog, args.profile) if args.profile else None
            output_json(
                {
                    "product": product,
                    "evaluation": evaluate_product(
                        catalog,
                        product,
                        profile,
                        as_of=parse_as_of(args.as_of),
                    ),
                }
            )
            return 0

        if args.command == "match":
            matches = match_products(
                catalog,
                asin=args.asin,
                part_number=args.part_number,
                model=args.model,
                title=args.title,
                category=args.category,
            )
            output_json({"count": len(matches), "matches": matches})
            return 0
        raise CatalogError(f"未対応のコマンド: {args.command}")
    except CatalogError as exc:
        output_json({"status": "error", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

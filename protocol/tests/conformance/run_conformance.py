#!/usr/bin/env python3
"""Repeatable conformance checks for the Stage 1 protocol artifacts."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError as exc:
    print("FAIL conformance dependency missing: install jsonschema>=4.26,<5", file=sys.stderr)
    raise SystemExit(2) from exc


HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parents[1]
SCHEMA_DIR = PACKAGE_ROOT / "schemas"
FIXTURE_DIR = HERE / "fixtures"

ENVELOPE_SCHEMA_PATH = SCHEMA_DIR / "envelope.schema.json"
VERDICT_SCHEMA_PATH = SCHEMA_DIR / "verdict.schema.json"
ALIASES_PATH = SCHEMA_DIR / "verdict-aliases.json"

CANONICAL_VERDICTS = ["通过", "带注通过", "条件通过", "不通过", "未核", "作废"]


class OutputVerdictError(ValueError):
    """Raised when a non-canonical verdict reaches an output boundary."""


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema, format_checker=FormatChecker())


def is_valid(instance: Any, schema: dict[str, Any]) -> bool:
    return not any(validator(schema).iter_errors(instance))


def normalize_historical_verdict(record: dict[str, Any], aliases: dict[str, Any]) -> dict[str, Any]:
    """Normalize aliases only while reading historical records."""
    alias_map = {
        item["historical_value"]: item["canonical_value"]
        for item in aliases["aliases"]
    }
    normalized = copy.deepcopy(record)
    value = normalized.get("verdict")
    if value in alias_map:
        normalized["verdict"] = alias_map[value]
    return normalized


def emit_verdict(record: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical copy or reject at the output boundary."""
    value = record.get("verdict")
    if value not in CANONICAL_VERDICTS:
        raise OutputVerdictError("output verdict must be one of the canonical six values")
    return copy.deepcopy(record)


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def expect_schema_valid(instance: Any, schema: dict[str, Any], label: str) -> None:
    errors = list(validator(schema).iter_errors(instance))
    assert_true(not errors, f"{label} should be valid; first error: {errors[0].message if errors else ''}")


def expect_schema_invalid(instance: Any, schema: dict[str, Any], label: str) -> None:
    errors = list(validator(schema).iter_errors(instance))
    assert_true(bool(errors), f"{label} should be rejected")


def expect_output_rejected(record: dict[str, Any]) -> None:
    try:
        emit_verdict(record)
    except OutputVerdictError:
        return
    raise AssertionError("output boundary accepted a non-canonical verdict")


def run_case(name: str, call: Callable[[], None], results: list[tuple[str, bool, str]]) -> None:
    try:
        call()
    except Exception as exc:
        results.append((name, False, str(exc)))
    else:
        results.append((name, True, ""))


def main() -> int:
    envelope_schema = load_json(ENVELOPE_SCHEMA_PATH)
    verdict_schema = load_json(VERDICT_SCHEMA_PATH)
    aliases = load_json(ALIASES_PATH)

    readonly_envelope = load_json(FIXTURE_DIR / "envelopes" / "readonly.json")
    external_envelope = load_json(FIXTURE_DIR / "envelopes" / "external-approved.json")
    missing_key_envelope = load_json(
        FIXTURE_DIR / "envelopes" / "invalid-missing-idempotency.json"
    )
    two_target_envelope = load_json(
        FIXTURE_DIR / "envelopes" / "invalid-two-targets.json"
    )

    pass_verdict = load_json(FIXTURE_DIR / "verdicts" / "pass.json")
    notes_verdict = load_json(FIXTURE_DIR / "verdicts" / "pass-with-notes.json")
    unverified_verdict = load_json(FIXTURE_DIR / "verdicts" / "unverified.json")
    invalid_unverified = load_json(
        FIXTURE_DIR / "verdicts" / "invalid-unverified-missing-exit.json"
    )
    historical_rejected = load_json(
        FIXTURE_DIR / "verdicts" / "historical-rejected.json"
    )
    output_rejected = load_json(FIXTURE_DIR / "verdicts" / "output-rejected.json")

    results: list[tuple[str, bool, str]] = []

    run_case(
        "schemas are valid Draft 2020-12 documents",
        lambda: (
            Draft202012Validator.check_schema(envelope_schema),
            Draft202012Validator.check_schema(verdict_schema),
        ),
        results,
    )
    run_case(
        "verdict schema enum is exactly the canonical six",
        lambda: assert_true(
            verdict_schema["properties"]["verdict"]["enum"] == CANONICAL_VERDICTS,
            "verdict schema enum differs from canonical values",
        ),
        results,
    )
    run_case(
        "alias table canonical values match schema",
        lambda: assert_true(
            aliases["canonical_values"] == CANONICAL_VERDICTS,
            "alias table and schema canonical values differ",
        ),
        results,
    )
    run_case(
        "historical alias is read-only and maps to canonical value",
        lambda: assert_true(
            aliases["aliases"]
            == [
                {
                    "historical_value": "驳回",
                    "canonical_value": "不通过",
                    "operation": "read_normalize_only",
                }
            ]
            and aliases["constraints"]["allow_on_read"] is True
            and aliases["constraints"]["allow_on_write"] is False
            and aliases["constraints"]["allow_in_output"] is False,
            "historical alias policy is not read-only",
        ),
        results,
    )
    run_case(
        "readonly envelope conforms",
        lambda: expect_schema_valid(readonly_envelope, envelope_schema, "readonly envelope"),
        results,
    )
    run_case(
        "approved external envelope conforms",
        lambda: expect_schema_valid(
            external_envelope, envelope_schema, "approved external envelope"
        ),
        results,
    )
    run_case(
        "envelope without idempotency key is rejected",
        lambda: expect_schema_invalid(
            missing_key_envelope, envelope_schema, "envelope without idempotency key"
        ),
        results,
    )
    run_case(
        "envelope with two targets is rejected",
        lambda: expect_schema_invalid(
            two_target_envelope, envelope_schema, "envelope with two targets"
        ),
        results,
    )
    run_case(
        "envelope fixtures stay below 256 KiB",
        lambda: assert_true(
            all(
                path.stat().st_size <= 262144
                for path in (FIXTURE_DIR / "envelopes").glob("*.json")
            ),
            "an envelope fixture exceeds 262144 bytes",
        ),
        results,
    )
    run_case(
        "pass verdict conforms",
        lambda: expect_schema_valid(pass_verdict, verdict_schema, "pass verdict"),
        results,
    )
    run_case(
        "pass-with-notes verdict conforms",
        lambda: expect_schema_valid(
            notes_verdict, verdict_schema, "pass-with-notes verdict"
        ),
        results,
    )
    run_case(
        "unverified verdict includes its exit fields",
        lambda: expect_schema_valid(
            unverified_verdict, verdict_schema, "unverified verdict"
        ),
        results,
    )
    run_case(
        "unverified verdict without exit fields is rejected",
        lambda: expect_schema_invalid(
            invalid_unverified, verdict_schema, "unverified verdict without exit"
        ),
        results,
    )
    run_case(
        "canonical schema rejects historical alias in raw form",
        lambda: expect_schema_invalid(
            historical_rejected, verdict_schema, "raw historical verdict"
        ),
        results,
    )

    normalized_historical = normalize_historical_verdict(historical_rejected, aliases)

    run_case(
        "historical alias normalizes to 不通过",
        lambda: assert_true(
            normalized_historical["verdict"] == "不通过",
            "historical verdict did not normalize to canonical value",
        ),
        results,
    )
    run_case(
        "normalized historical verdict conforms",
        lambda: expect_schema_valid(
            normalized_historical, verdict_schema, "normalized historical verdict"
        ),
        results,
    )
    run_case(
        "output boundary rejects historical alias",
        lambda: expect_output_rejected(output_rejected),
        results,
    )

    for name, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
    passed_count = sum(1 for _, passed, _ in results if passed)
    total = len(results)
    print(f"SUMMARY {passed_count}/{total} PASS" if passed_count == total else f"SUMMARY {passed_count}/{total} FAIL")
    return 0 if passed_count == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import math
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any


_STATION_ID = re.compile(r"^[0-9]{5}$")
_DECIMAL_VALUE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


class ObservationContractError(ValueError):
    """Raised when a stream observation violates the published contract."""


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ObservationContractError(f"{field} must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservationContractError(f"{field} is not a valid timestamp") from exc
    if parsed.utcoffset() is None:
        raise ObservationContractError(f"{field} must include a timezone")
    return value


def _station_id(value: Any) -> str:
    if not isinstance(value, str) or _STATION_ID.fullmatch(value) is None:
        raise ObservationContractError("station_id must be a five-digit string")
    return value


def _v1_demand(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObservationContractError("v1 demand must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ObservationContractError("v1 demand must be finite and non-negative")
    return value


def _v2_demand(value: Any) -> float:
    if not isinstance(value, str) or _DECIMAL_VALUE.fullmatch(value) is None:
        raise ObservationContractError(
            "v2 measurement.value must be a decimal string without separators"
        )
    try:
        numeric = Decimal(value)
    except InvalidOperation as exc:
        raise ObservationContractError("v2 measurement.value is not decimal") from exc
    if not numeric.is_finite() or numeric < 0:
        raise ObservationContractError(
            "v2 measurement.value must be finite and non-negative"
        )
    return float(numeric)


def normalize_stream_observation(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a v1 or v2 stream row into the durable database contract."""
    if not isinstance(row, dict):
        raise ObservationContractError("stream observation must be an object")

    station_id = _station_id(row.get("station_id"))
    observed_at = _timestamp(row.get("observed_at"), "observed_at")
    released_at = _timestamp(row.get("released_at"), "released_at")
    schema_version = row.get("schema_version")

    if schema_version is None:
        return {
            "station_id": station_id,
            "observed_at": observed_at,
            "released_at": released_at,
            "demand": _v1_demand(row.get("demand")),
            "source_schema_version": 1,
            "quality": "observed",
            "unit": "passengers",
        }

    if schema_version != 2:
        raise ObservationContractError(
            f"unsupported observation schema_version: {schema_version!r}"
        )
    measurement = row.get("measurement")
    if not isinstance(measurement, dict):
        raise ObservationContractError("v2 measurement must be an object")
    quality = measurement.get("quality")
    unit = measurement.get("unit")
    value = measurement.get("value")
    if quality not in {"observed", "missing"}:
        raise ObservationContractError("v2 quality must be observed or missing")
    if unit != "passengers":
        raise ObservationContractError("v2 unit must be passengers")
    if quality == "missing":
        if value is not None:
            raise ObservationContractError(
                "v2 missing observations must have a null measurement.value"
            )
        demand: float | None = None
    else:
        demand = _v2_demand(value)

    return {
        "station_id": station_id,
        "observed_at": observed_at,
        "released_at": released_at,
        "demand": demand,
        "source_schema_version": 2,
        "quality": quality,
        "unit": unit,
    }


def normalize_stream_page(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_stream_observation(row) for row in rows]


def stream_source_version(rows: list[dict[str, Any]]) -> str:
    versions = {int(row["source_schema_version"]) for row in rows}
    if not versions:
        return "competition-stream-empty"
    suffix = "-".join(f"v{version}" for version in sorted(versions))
    return f"competition-stream-{suffix}"

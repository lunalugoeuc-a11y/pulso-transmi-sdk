from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor

from pulso_transmi.client import PulsoTransmiClient, PulsoTransmiError
from pulso_transmi.store import SupabaseStore


EXTRA_TREES_LAGS = (96, 192, 288, 384, 480, 576, 672, 768, 1344)
EXTRA_TREES_FEATURES = (
    "station_code",
    "slot_sin",
    "slot_cos",
    "dow_sin",
    "dow_cos",
    "is_weekend",
    *(f"lag_{lag}" for lag in EXTRA_TREES_LAGS),
    "daily_median",
    "daily_mean",
    "daily_trend",
    "weekly_trend",
)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError(f"timestamp without timezone: {value}")
    return parsed.astimezone(timezone.utc)


def git_commit() -> str:
    value = os.getenv("GITHUB_SHA")
    if value and 7 <= len(value) <= 40 and all(c in "0123456789abcdefABCDEF" for c in value):
        return value.lower()
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("cannot determine the Git commit") from exc
    if len(value) != 40:
        raise RuntimeError("Git returned an invalid commit")
    return value.lower()


def sync_stream(
    api: PulsoTransmiClient, store: SupabaseStore, run_id: str
) -> int:
    cursor = store.collector_cursor()
    seen: set[str] = set()
    processed = 0
    while True:
        page = api.stream_observations_page(cursor=cursor, limit=5000)
        rows = page.get("data", [])
        next_cursor = page.get("next_cursor")
        processed += store.ingest_observation_page(run_id, next_cursor, rows)
        if next_cursor is None:
            return processed
        if next_cursor == cursor or next_cursor in seen:
            raise RuntimeError("competition API returned a repeated stream cursor")
        seen.add(next_cursor)
        cursor = next_cursor


def seasonal_naive_predictions(
    history: list[dict[str, Any]], cycle: dict[str, Any], *, lag_days: int = 1
) -> list[dict[str, Any]]:
    """Predict each target from the same station at a prior daily/weekly season."""
    if lag_days < 1:
        raise ValueError("lag_days must be positive")
    cutoff = _timestamp(cycle["data_cutoff"])
    by_station: dict[str, list[tuple[datetime, float]]] = {}
    for row in history:
        observed_at = _timestamp(row["observed_at"])
        demand = float(row["demand"])
        if observed_at <= cutoff and math.isfinite(demand) and demand >= 0:
            by_station.setdefault(row["station_id"], []).append((observed_at, demand))
    for values in by_station.values():
        values.sort()

    predictions = []
    for target in cycle["targets"]:
        station_id = target["station_id"]
        target_at = _timestamp(target["target_at"])
        values = by_station.get(station_id, [])
        exact = {observed_at: demand for observed_at, demand in values}
        value = exact.get(target_at - timedelta(days=lag_days))
        if value is None and values:
            value = values[-1][1]
        if value is None:
            raise RuntimeError(f"not enough history for station {station_id}")
        predictions.append(
            {
                "station_id": station_id,
                "target_at": target["target_at"],
                "value": round(min(100_000.0, max(0.0, value)), 3),
            }
        )
    return predictions


def hybrid_seasonal_predictions(
    history: list[dict[str, Any]], cycle: dict[str, Any]
) -> list[dict[str, Any]]:
    """Blend the daily and weekly seasonal values with equal weight."""
    cutoff = _timestamp(cycle["data_cutoff"])
    by_station: dict[str, dict[datetime, float]] = {}
    latest: dict[str, float] = {}
    for row in sorted(history, key=lambda item: _timestamp(item["observed_at"])):
        observed_at = _timestamp(row["observed_at"])
        demand = float(row["demand"])
        if observed_at <= cutoff and math.isfinite(demand) and demand >= 0:
            station_id = row["station_id"]
            by_station.setdefault(station_id, {})[observed_at] = demand
            latest[station_id] = demand

    predictions = []
    for target in cycle["targets"]:
        station_id = target["station_id"]
        target_at = _timestamp(target["target_at"])
        values = by_station.get(station_id, {})
        daily = values.get(target_at - timedelta(days=1))
        weekly = values.get(target_at - timedelta(days=7))
        available = [value for value in (daily, weekly) if value is not None]
        value = sum(available) / len(available) if available else latest.get(station_id)
        if value is None:
            raise RuntimeError(f"not enough history for station {station_id}")
        predictions.append(
            {
                "station_id": station_id,
                "target_at": target["target_at"],
                "value": round(min(100_000.0, max(0.0, value)), 3),
            }
        )
    return predictions


def extra_trees_predictions(
    history: list[dict[str, Any]], cycle: dict[str, Any]
) -> list[dict[str, Any]]:
    """Train an as-of-cutoff Extra Trees model and predict the exact target set.

    Every lag is at least one day, so all feature values for the four forecast
    horizons already exist at ``data_cutoff``. This prevents target leakage.
    """
    cutoff = _timestamp(cycle["data_cutoff"])
    frame = pd.DataFrame(history)
    if frame.empty:
        raise RuntimeError("not enough history to train Extra Trees")
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
    frame["demand"] = pd.to_numeric(frame["demand"], errors="coerce")
    frame = frame.loc[frame["observed_at"] <= cutoff].dropna(subset=["demand"])
    frame = frame.sort_values(["station_id", "observed_at"]).drop_duplicates(
        ["station_id", "observed_at"], keep="last"
    )
    stations = sorted(frame["station_id"].astype(str).unique())
    station_codes = {station_id: index for index, station_id in enumerate(stations)}
    frame["station_id"] = frame["station_id"].astype(str)
    frame["station_code"] = frame["station_id"].map(station_codes)
    local_time = frame["observed_at"].dt.tz_convert("America/Bogota")
    slot = local_time.dt.hour * 4 + local_time.dt.minute // 15
    frame["slot_sin"] = np.sin(2 * np.pi * slot / 96)
    frame["slot_cos"] = np.cos(2 * np.pi * slot / 96)
    frame["dow_sin"] = np.sin(2 * np.pi * local_time.dt.dayofweek / 7)
    frame["dow_cos"] = np.cos(2 * np.pi * local_time.dt.dayofweek / 7)
    frame["is_weekend"] = (local_time.dt.dayofweek >= 5).astype(int)
    grouped = frame.groupby("station_id", observed=True)["demand"]
    for lag in EXTRA_TREES_LAGS:
        frame[f"lag_{lag}"] = grouped.shift(lag)
    daily_columns = [f"lag_{lag}" for lag in EXTRA_TREES_LAGS[:6]]
    frame["daily_median"] = frame[daily_columns].median(axis=1)
    frame["daily_mean"] = frame[daily_columns].mean(axis=1)
    frame["daily_trend"] = frame["lag_96"] - frame["lag_192"]
    frame["weekly_trend"] = frame["lag_672"] - frame["lag_1344"]
    training = frame.dropna(subset=[*EXTRA_TREES_FEATURES, "demand"])
    if len(training) < 1000:
        raise RuntimeError("not enough complete history to train Extra Trees")

    model = ExtraTreesRegressor(
        n_estimators=240,
        min_samples_leaf=8,
        max_features=0.8,
        n_jobs=-1,
        random_state=42,
    )
    model.fit(training[list(EXTRA_TREES_FEATURES)], training["demand"])

    lookup = {
        (row.station_id, row.observed_at.to_pydatetime()): float(row.demand)
        for row in frame[["station_id", "observed_at", "demand"]].itertuples(index=False)
    }
    feature_rows: list[dict[str, Any]] = []
    for target in cycle["targets"]:
        station_id = str(target["station_id"])
        target_at = _timestamp(target["target_at"])
        if station_id not in station_codes:
            raise RuntimeError(f"unknown station {station_id}")
        local_target = target_at.astimezone().astimezone(
            timezone(timedelta(hours=-5))
        )
        slot_number = local_target.hour * 4 + local_target.minute // 15
        row: dict[str, Any] = {
            "station_code": station_codes[station_id],
            "slot_sin": math.sin(2 * math.pi * slot_number / 96),
            "slot_cos": math.cos(2 * math.pi * slot_number / 96),
            "dow_sin": math.sin(2 * math.pi * local_target.weekday() / 7),
            "dow_cos": math.cos(2 * math.pi * local_target.weekday() / 7),
            "is_weekend": int(local_target.weekday() >= 5),
        }
        for lag in EXTRA_TREES_LAGS:
            value = lookup.get((station_id, target_at - timedelta(minutes=15 * lag)))
            if value is None:
                raise RuntimeError(f"missing lag {lag} for station {station_id}")
            row[f"lag_{lag}"] = value
        daily_values = [row[column] for column in daily_columns]
        row["daily_median"] = float(np.median(daily_values))
        row["daily_mean"] = float(np.mean(daily_values))
        row["daily_trend"] = row["lag_96"] - row["lag_192"]
        row["weekly_trend"] = row["lag_672"] - row["lag_1344"]
        feature_rows.append(row)

    values = model.predict(pd.DataFrame(feature_rows)[list(EXTRA_TREES_FEATURES)])
    return [
        {
            "station_id": target["station_id"],
            "target_at": target["target_at"],
            "value": round(min(100_000.0, max(0.0, float(value))), 3),
        }
        for target, value in zip(cycle["targets"], values, strict=True)
    ]


def validate_exact_targets(
    predictions: list[dict[str, Any]], cycle: dict[str, Any]
) -> None:
    expected = {
        (target["station_id"], _timestamp(target["target_at"]))
        for target in cycle["targets"]
    }
    actual = {
        (prediction["station_id"], _timestamp(prediction["target_at"]))
        for prediction in predictions
    }
    if len(predictions) != len(actual):
        raise ValueError("predictions contain duplicate targets")
    if actual != expected or len(predictions) != cycle["expected_predictions"]:
        missing = len(expected - actual)
        extra = len(actual - expected)
        raise ValueError(f"target contract mismatch: missing={missing}, extra={extra}")
    for prediction in predictions:
        value = prediction["value"]
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("prediction values must be finite numbers")
        if not 0 <= value <= 100_000:
            raise ValueError("prediction values must be between 0 and 100000")


def _training_end(model: dict[str, Any]) -> str:
    training = model["training_runs"]
    if isinstance(training, list):
        training = training[0]
    return training["train_end"]


def _seasonal_lag_days(model: dict[str, Any]) -> int:
    algorithm = model["algorithm"].lower()
    if "lag 672" in algorithm or "weekly" in algorithm:
        return 7
    if "lag 96" in algorithm or "daily" in algorithm:
        return 1
    raise RuntimeError(f"unsupported promoted algorithm: {model['algorithm']}")


def build_payload(
    cycle: dict[str, Any],
    model: dict[str, Any],
    predictions: list[dict[str, Any]],
    commit_sha: str,
) -> tuple[dict[str, Any], str, str, str]:
    stable_run_material = f"{cycle['cycle_id']}:{model['version']}:{commit_sha}"
    client_run_id = "ptm-" + hashlib.sha256(stable_run_material.encode()).hexdigest()[:40]
    payload = {
        "schema_version": "1.0",
        "cycle_id": cycle["cycle_id"],
        "client_run_id": client_run_id,
        "data_cutoff": cycle["data_cutoff"],
        "model": {
            "version": model["version"],
            "trained_at": model["created_at"],
            "training_data_end": _training_end(model),
            "git_commit": commit_sha,
        },
        "predictions": predictions,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
    idempotency_key = "ptm-" + payload_hash.removeprefix("sha256:")[:48]
    return payload, client_run_id, idempotency_key, payload_hash


def run_pipeline(
    api: PulsoTransmiClient, store: SupabaseStore, *, trigger: str = "manual"
) -> str:
    commit_sha = git_commit()
    run_id = store.create_run("full", commit_sha, trigger)
    stage = "sync"
    try:
        synced = sync_stream(api, store, run_id)
        print(f"collector: {synced} rows processed")

        stage = "cycle"
        cycle = api.current_cycle()
        if cycle is None:
            store.finish_run(run_id, "succeeded")
            print("cycle: no_open_cycle")
            return "no_open_cycle"
        store.save_cycle(cycle)

        if datetime.now(timezone.utc) >= _timestamp(cycle["closes_at"]):
            store.finish_run(run_id, "succeeded")
            print(f"cycle: {cycle['cycle_id']} already closed")
            return "cycle_closed"

        stage = "model"
        model = store.active_model()
        algorithm = model["algorithm"].lower()
        is_extra_trees = "extra trees" in algorithm
        is_hybrid = "hybrid" in algorithm and "lag 96" in algorithm and "lag 672" in algorithm
        lag_days = 14 if is_extra_trees else (7 if is_hybrid else _seasonal_lag_days(model))
        if store.accepted_receipt_exists(cycle["cycle_id"], model["model_id"]):
            store.finish_run(run_id, "succeeded")
            print(f"cycle: {cycle['cycle_id']} already submitted")
            return "already_submitted"

        stage = "inference"
        station_ids = sorted({target["station_id"] for target in cycle["targets"]})
        history = store.history(
            station_ids,
            cycle["data_cutoff"],
            points=5000 if is_extra_trees else lag_days * 96 + 96,
        )
        if is_extra_trees:
            try:
                predictions = extra_trees_predictions(history, cycle)
            except RuntimeError as exc:
                # The competition stream can start with only a few days of
                # retained observations. Extra Trees needs fourteen complete
                # days for lag_1344, so recover the public historical window
                # from the paginated observations endpoint when Supabase does
                # not yet contain enough history. The endpoint is still
                # bounded by the official cycle cutoff.
                if "not enough complete history" not in str(exc):
                    raise
                historical = api.observations_dataframe(end=cycle["data_cutoff"])
                history = historical.to_dict(orient="records")
                predictions = extra_trees_predictions(history, cycle)
        elif is_hybrid:
            predictions = hybrid_seasonal_predictions(history, cycle)
        else:
            predictions = seasonal_naive_predictions(history, cycle, lag_days=lag_days)
        validate_exact_targets(predictions, cycle)
        payload, client_run_id, idempotency_key, payload_hash = build_payload(
            cycle, model, predictions, commit_sha
        )
        if len(json.dumps(payload, separators=(",", ":")).encode()) > 64 * 1024:
            raise ValueError("submission payload exceeds 64 KB")
        prediction_ids = store.save_predictions(
            run_id=run_id,
            model_id=model["model_id"],
            cycle_id=cycle["cycle_id"],
            predictions=predictions,
            targets=cycle["targets"],
        )

        stage = "submission"
        receipt = api.submit(payload, idempotency_key=idempotency_key)
        store.save_receipt(
            run_id=run_id,
            model_id=model["model_id"],
            commit_sha=commit_sha,
            cycle_id=cycle["cycle_id"],
            client_run_id=client_run_id,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            receipt=receipt,
            prediction_ids=prediction_ids,
        )
        store.finish_run(run_id, "succeeded")
        print(
            f"submission: {receipt['submission_id']} "
            f"({receipt['predictions_received']}/{receipt['expected_predictions']})"
        )
        return "submitted"
    except Exception as exc:
        retryable = isinstance(exc, PulsoTransmiError) and (
            exc.status_code == 429 or (exc.status_code or 0) >= 500
        )
        try:
            store.record_error(run_id, stage, exc, retryable=retryable)
            store.finish_run(run_id, "failed")
        except Exception:
            pass
        raise


def main() -> None:
    if not os.getenv("PULSO_API_KEY"):
        raise SystemExit("PULSO_API_KEY is required")
    trigger = os.getenv("GITHUB_EVENT_NAME", "manual")
    with PulsoTransmiClient() as api, SupabaseStore() as store:
        run_pipeline(api, store, trigger=trigger)


if __name__ == "__main__":
    main()

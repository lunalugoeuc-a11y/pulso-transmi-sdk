from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

from pulso_transmi.client import PulsoTransmiClient, PulsoTransmiError
from pulso_transmi.store import SupabaseStore


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
        lag_days = _seasonal_lag_days(model)
        if store.accepted_receipt_exists(cycle["cycle_id"], model["model_id"]):
            store.finish_run(run_id, "succeeded")
            print(f"cycle: {cycle['cycle_id']} already submitted")
            return "already_submitted"

        stage = "inference"
        station_ids = sorted({target["station_id"] for target in cycle["targets"]})
        history = store.history(
            station_ids, cycle["data_cutoff"], points=lag_days * 96 + 96
        )
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

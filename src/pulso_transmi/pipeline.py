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
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor

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
HGB_PROFILE_FEATURES = EXTRA_TREES_FEATURES
HGB_PROFILE_WEIGHT = 0.40
EXTRA_TREES_STACK_WEIGHT = 0.75
DRIFT_ADAPTIVE_FEATURES = (
    "station_code",
    "horizon_steps",
    "slot_sin",
    "slot_cos",
    "dow_sin",
    "dow_cos",
    "is_weekend",
    "lag_cutoff",
    "lag_1h_before_cutoff",
    "lag_3h_before_cutoff",
    "lag_1d",
    "lag_2d",
    "lag_7d",
    "recent_change_1h",
    "level_change_1d",
    "level_change_7d",
)
DRIFT_ADAPTIVE_TRAINING_DAYS = 21
DRIFT_ADAPTIVE_HALF_LIFE_DAYS = 3.0
DRIFT_HORIZON_FEATURES = tuple(
    feature for feature in DRIFT_ADAPTIVE_FEATURES if feature != "horizon_steps"
)
DRIFT_HORIZON_PERSISTENCE_WEIGHT_60M = 0.15


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


def adaptive_profile_predictions(
    history: list[dict[str, Any]], cycle: dict[str, Any], *, half_life_days: float = 14.0
) -> list[dict[str, Any]]:
    """Exponentially weight prior matching weekday/slot observations."""
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    cutoff = _timestamp(cycle["data_cutoff"])
    profiles: dict[tuple[str, int, int], list[tuple[datetime, float]]] = {}
    slot_profiles: dict[tuple[str, int], list[tuple[datetime, float]]] = {}
    latest: dict[str, float] = {}
    for row in sorted(history, key=lambda item: _timestamp(item["observed_at"])):
        observed_at = _timestamp(row["observed_at"])
        demand = float(row["demand"])
        if observed_at > cutoff or not math.isfinite(demand) or demand < 0:
            continue
        station_id = str(row["station_id"])
        local = pd.Timestamp(observed_at).tz_convert("America/Bogota")
        slot = int(local.hour * 4 + local.minute // 15)
        profiles.setdefault((station_id, int(local.dayofweek), slot), []).append(
            (observed_at, demand)
        )
        slot_profiles.setdefault((station_id, slot), []).append((observed_at, demand))
        latest[station_id] = demand

    predictions: list[dict[str, Any]] = []
    for target in cycle["targets"]:
        station_id = str(target["station_id"])
        target_at = _timestamp(target["target_at"])
        local_target = pd.Timestamp(target_at).tz_convert("America/Bogota")
        slot = int(local_target.hour * 4 + local_target.minute // 15)
        values = profiles.get((station_id, int(local_target.dayofweek), slot), [])
        if not values:
            values = slot_profiles.get((station_id, slot), [])
        if values:
            weights = np.asarray(
                [
                    0.5
                    ** (
                        max(0.0, (target_at - observed_at).total_seconds() / 86400)
                        / half_life_days
                    )
                    for observed_at, _ in values
                ]
            )
            value = float(
                np.average(np.asarray([demand for _, demand in values]), weights=weights)
            )
        else:
            value = latest.get(station_id, math.nan)
        if not math.isfinite(value):
            raise RuntimeError(f"not enough history for station {station_id}")
        predictions.append(
            {
                "station_id": target["station_id"],
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


def hgb_profile_predictions(
    history: list[dict[str, Any]], cycle: dict[str, Any]
) -> list[dict[str, Any]]:
    """Blend a leakage-safe histogram GBM with a historical demand profile."""
    cutoff = _timestamp(cycle["data_cutoff"])
    frame = pd.DataFrame(history)
    if frame.empty:
        raise RuntimeError("not enough history to train HGB profile")
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
    frame["demand"] = pd.to_numeric(frame["demand"], errors="coerce")
    frame = frame.loc[frame["observed_at"] <= cutoff].dropna(subset=["demand"])
    frame = frame.sort_values(["station_id", "observed_at"]).drop_duplicates(
        ["station_id", "observed_at"], keep="last"
    )
    frame["station_id"] = frame["station_id"].astype(str)
    stations = sorted(frame["station_id"].unique())
    station_codes = {station_id: index for index, station_id in enumerate(stations)}
    frame["station_code"] = frame["station_id"].map(station_codes)
    local_time = frame["observed_at"].dt.tz_convert("America/Bogota")
    frame["local_dow"] = local_time.dt.dayofweek
    frame["local_slot"] = local_time.dt.hour * 4 + local_time.dt.minute // 15
    frame["slot_sin"] = np.sin(2 * np.pi * frame["local_slot"] / 96)
    frame["slot_cos"] = np.cos(2 * np.pi * frame["local_slot"] / 96)
    frame["dow_sin"] = np.sin(2 * np.pi * frame["local_dow"] / 7)
    frame["dow_cos"] = np.cos(2 * np.pi * frame["local_dow"] / 7)
    frame["is_weekend"] = (frame["local_dow"] >= 5).astype(int)
    grouped = frame.groupby("station_id", observed=True)["demand"]
    for lag in EXTRA_TREES_LAGS:
        frame[f"lag_{lag}"] = grouped.shift(lag)
    daily_columns = [f"lag_{lag}" for lag in EXTRA_TREES_LAGS[:6]]
    frame["daily_median"] = frame[daily_columns].median(axis=1)
    frame["daily_mean"] = frame[daily_columns].mean(axis=1)
    frame["daily_trend"] = frame["lag_96"] - frame["lag_192"]
    frame["weekly_trend"] = frame["lag_672"] - frame["lag_1344"]
    training = frame.dropna(subset=[*HGB_PROFILE_FEATURES, "demand"])
    if len(training) < 1000:
        raise RuntimeError("not enough complete history to train HGB profile")

    model = HistGradientBoostingRegressor(
        learning_rate=0.05,
        max_iter=400,
        max_leaf_nodes=31,
        min_samples_leaf=20,
        l2_regularization=1.0,
        random_state=42,
    )
    model.fit(training[list(HGB_PROFILE_FEATURES)], training["demand"])
    profile = training.groupby(
        ["station_id", "local_dow", "local_slot"], observed=True
    )["demand"].median()
    station_profile = training.groupby("station_id", observed=True)["demand"].median()
    global_profile = float(training["demand"].median())
    lookup = {
        (row.station_id, row.observed_at.to_pydatetime()): float(row.demand)
        for row in frame[["station_id", "observed_at", "demand"]].itertuples(index=False)
    }

    feature_rows: list[dict[str, Any]] = []
    profile_values: list[float] = []
    for target in cycle["targets"]:
        station_id = str(target["station_id"])
        target_at = _timestamp(target["target_at"])
        if station_id not in station_codes:
            raise RuntimeError(f"unknown station {station_id}")
        local_target = pd.Timestamp(target_at).tz_convert("America/Bogota")
        local_dow = int(local_target.dayofweek)
        local_slot = int(local_target.hour * 4 + local_target.minute // 15)
        row: dict[str, Any] = {
            "station_code": station_codes[station_id],
            "slot_sin": math.sin(2 * math.pi * local_slot / 96),
            "slot_cos": math.cos(2 * math.pi * local_slot / 96),
            "dow_sin": math.sin(2 * math.pi * local_dow / 7),
            "dow_cos": math.cos(2 * math.pi * local_dow / 7),
            "is_weekend": int(local_dow >= 5),
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
        profile_values.append(
            float(
                profile.get(
                    (station_id, local_dow, local_slot),
                    station_profile.get(station_id, global_profile),
                )
            )
        )

    hgb_values = model.predict(
        pd.DataFrame(feature_rows)[list(HGB_PROFILE_FEATURES)]
    )
    hgb_profile_values = (
        (1.0 - HGB_PROFILE_WEIGHT) * hgb_values
        + HGB_PROFILE_WEIGHT * np.asarray(profile_values)
    )
    extra_trees = ExtraTreesRegressor(
        n_estimators=240,
        min_samples_leaf=8,
        max_features=0.8,
        n_jobs=-1,
        random_state=42,
    )
    extra_trees.fit(training[list(HGB_PROFILE_FEATURES)], training["demand"])
    extra_trees_values = extra_trees.predict(
        pd.DataFrame(feature_rows)[list(HGB_PROFILE_FEATURES)]
    )
    values = (
        EXTRA_TREES_STACK_WEIGHT * extra_trees_values
        + (1.0 - EXTRA_TREES_STACK_WEIGHT) * hgb_profile_values
    )
    return [
        {
            "station_id": target["station_id"],
            "target_at": target["target_at"],
            "value": round(min(100_000.0, max(0.0, float(value))), 3),
        }
        for target, value in zip(cycle["targets"], values, strict=True)
    ]


def drift_adaptive_predictions(
    history: list[dict[str, Any]], cycle: dict[str, Any]
) -> list[dict[str, Any]]:
    """Retrain a recency-weighted model that can react to abrupt level shifts.

    The four forecast horizons are trained explicitly. For a historical target
    at time ``t`` and horizon ``h``, ``lag_cutoff`` is the value at ``t-h``;
    therefore every feature was already observable at that simulated cutoff.
    Short lags make the model responsive immediately after drift instead of
    waiting a full day for lag 96 to enter the new regime.
    """
    return _drift_adaptive_predictions(history, cycle, separate_horizons=False)


def drift_adaptive_horizon_predictions(
    history: list[dict[str, Any]], cycle: dict[str, Any]
) -> list[dict[str, Any]]:
    """Fit one leakage-safe model per horizon and stabilize the 60m forecast.

    Separating the estimators prevents the abundant short-horizon patterns from
    dominating the weakest 60-minute horizon. The final 60-minute estimate is
    blended conservatively with the last value known at the official cutoff.
    """
    return _drift_adaptive_predictions(history, cycle, separate_horizons=True)


def _drift_adaptive_predictions(
    history: list[dict[str, Any]],
    cycle: dict[str, Any],
    *,
    separate_horizons: bool,
) -> list[dict[str, Any]]:
    cutoff = _timestamp(cycle["data_cutoff"])
    frame = pd.DataFrame(history)
    if frame.empty:
        raise RuntimeError("not enough history to train drift-adaptive model")
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
    frame["demand"] = pd.to_numeric(frame["demand"], errors="coerce")
    frame = frame.loc[frame["observed_at"] <= cutoff].dropna(subset=["demand"])
    frame = frame.sort_values(["station_id", "observed_at"]).drop_duplicates(
        ["station_id", "observed_at"], keep="last"
    )
    frame["station_id"] = frame["station_id"].astype(str)
    stations = sorted(frame["station_id"].unique())
    station_codes = {station_id: index for index, station_id in enumerate(stations)}
    frame["station_code"] = frame["station_id"].map(station_codes)
    local_time = frame["observed_at"].dt.tz_convert("America/Bogota")
    frame["slot_sin"] = np.sin(
        2 * np.pi * (local_time.dt.hour * 4 + local_time.dt.minute // 15) / 96
    )
    frame["slot_cos"] = np.cos(
        2 * np.pi * (local_time.dt.hour * 4 + local_time.dt.minute // 15) / 96
    )
    frame["dow_sin"] = np.sin(2 * np.pi * local_time.dt.dayofweek / 7)
    frame["dow_cos"] = np.cos(2 * np.pi * local_time.dt.dayofweek / 7)
    frame["is_weekend"] = (local_time.dt.dayofweek >= 5).astype(int)

    grouped = frame.groupby("station_id", observed=True)["demand"]
    lag_columns: dict[int, pd.Series] = {
        lag: grouped.shift(lag)
        for lag in (1, 2, 3, 4, 5, 6, 7, 8, 12, 13, 14, 15, 16, 96, 192, 672)
    }
    training_parts: list[pd.DataFrame] = []
    for horizon_steps in range(1, 5):
        part = frame[
            [
                "observed_at",
                "demand",
                "station_code",
                "slot_sin",
                "slot_cos",
                "dow_sin",
                "dow_cos",
                "is_weekend",
            ]
        ].copy()
        part["horizon_steps"] = horizon_steps
        part["lag_cutoff"] = lag_columns[horizon_steps]
        part["lag_1h_before_cutoff"] = lag_columns[horizon_steps + 4]
        part["lag_3h_before_cutoff"] = lag_columns[horizon_steps + 12]
        part["lag_1d"] = lag_columns[96]
        part["lag_2d"] = lag_columns[192]
        part["lag_7d"] = lag_columns[672]
        part["recent_change_1h"] = (
            part["lag_cutoff"] - part["lag_1h_before_cutoff"]
        )
        part["level_change_1d"] = part["lag_cutoff"] - part["lag_1d"]
        part["level_change_7d"] = part["lag_cutoff"] - part["lag_7d"]
        training_parts.append(part)
    training = pd.concat(training_parts, ignore_index=True).dropna(
        subset=[*DRIFT_ADAPTIVE_FEATURES, "demand"]
    )
    window_start = pd.Timestamp(
        cutoff - timedelta(days=DRIFT_ADAPTIVE_TRAINING_DAYS)
    )
    training = training.loc[training["observed_at"] >= window_start]
    if len(training) < 1000:
        raise RuntimeError("not enough complete history to train drift-adaptive model")
    age_days = (
        pd.Timestamp(cutoff) - training["observed_at"]
    ).dt.total_seconds() / 86400
    sample_weight = np.power(0.5, age_days / DRIFT_ADAPTIVE_HALF_LIFE_DAYS)
    lookup = {
        (row.station_id, row.observed_at.to_pydatetime()): float(row.demand)
        for row in frame[["station_id", "observed_at", "demand"]].itertuples(
            index=False
        )
    }
    feature_rows: list[dict[str, Any]] = []
    for target in cycle["targets"]:
        station_id = str(target["station_id"])
        target_at = _timestamp(target["target_at"])
        horizon_steps = int(target["horizon_minutes"]) // 15
        if station_id not in station_codes or horizon_steps not in range(1, 5):
            raise RuntimeError(f"unsupported target for station {station_id}")
        local_target = pd.Timestamp(target_at).tz_convert("America/Bogota")
        local_slot = int(local_target.hour * 4 + local_target.minute // 15)

        def demand_at(lag_steps: int) -> float:
            value = lookup.get(
                (station_id, target_at - timedelta(minutes=15 * lag_steps))
            )
            if value is None:
                raise RuntimeError(
                    f"missing lag {lag_steps} for station {station_id}"
                )
            return value

        lag_cutoff = demand_at(horizon_steps)
        lag_1h = demand_at(horizon_steps + 4)
        lag_3h = demand_at(horizon_steps + 12)
        lag_1d = demand_at(96)
        lag_2d = demand_at(192)
        lag_7d = demand_at(672)
        feature_rows.append(
            {
                "station_code": station_codes[station_id],
                "horizon_steps": horizon_steps,
                "slot_sin": math.sin(2 * math.pi * local_slot / 96),
                "slot_cos": math.cos(2 * math.pi * local_slot / 96),
                "dow_sin": math.sin(2 * math.pi * int(local_target.dayofweek) / 7),
                "dow_cos": math.cos(2 * math.pi * int(local_target.dayofweek) / 7),
                "is_weekend": int(local_target.dayofweek >= 5),
                "lag_cutoff": lag_cutoff,
                "lag_1h_before_cutoff": lag_1h,
                "lag_3h_before_cutoff": lag_3h,
                "lag_1d": lag_1d,
                "lag_2d": lag_2d,
                "lag_7d": lag_7d,
                "recent_change_1h": lag_cutoff - lag_1h,
                "level_change_1d": lag_cutoff - lag_1d,
                "level_change_7d": lag_cutoff - lag_7d,
            }
        )
    target_features = pd.DataFrame(feature_rows)
    if separate_horizons:
        values = np.empty(len(target_features), dtype=float)
        for horizon_steps in sorted(target_features["horizon_steps"].unique()):
            train_mask = training["horizon_steps"] == horizon_steps
            target_mask = target_features["horizon_steps"] == horizon_steps
            if int(train_mask.sum()) < 250:
                raise RuntimeError(
                    f"not enough history for horizon {15 * int(horizon_steps)}m"
                )
            model = ExtraTreesRegressor(
                n_estimators=200,
                min_samples_leaf=5,
                max_features=0.9,
                n_jobs=-1,
                random_state=42 + int(horizon_steps),
            )
            model.fit(
                training.loc[train_mask, list(DRIFT_HORIZON_FEATURES)],
                training.loc[train_mask, "demand"],
                sample_weight=sample_weight[train_mask.to_numpy()],
            )
            horizon_values = model.predict(
                target_features.loc[target_mask, list(DRIFT_HORIZON_FEATURES)]
            )
            if int(horizon_steps) == 4:
                persistence = target_features.loc[target_mask, "lag_cutoff"].to_numpy()
                horizon_values = (
                    (1.0 - DRIFT_HORIZON_PERSISTENCE_WEIGHT_60M) * horizon_values
                    + DRIFT_HORIZON_PERSISTENCE_WEIGHT_60M * persistence
                )
            values[target_mask.to_numpy()] = horizon_values
    else:
        model = ExtraTreesRegressor(
            n_estimators=240,
            min_samples_leaf=5,
            max_features=0.9,
            n_jobs=-1,
            random_state=42,
        )
        model.fit(
            training[list(DRIFT_ADAPTIVE_FEATURES)],
            training["demand"],
            sample_weight=sample_weight,
        )
        values = model.predict(target_features[list(DRIFT_ADAPTIVE_FEATURES)])
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

        stage = "evaluation"
        evaluated = store.evaluate_available_predictions()
        print(f"evaluation: {evaluated} predictions evaluated")

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
        is_drift_horizon = "drift adaptive horizon" in algorithm
        is_drift_adaptive = "drift adaptive" in algorithm
        is_hgb_profile = "hgb" in algorithm and "profile" in algorithm
        is_adaptive_profile = "adaptive profile" in algorithm and "hl14" in algorithm
        is_hybrid = "hybrid" in algorithm and "lag 96" in algorithm and "lag 672" in algorithm
        lag_days = 45 if is_adaptive_profile else (14 if (is_extra_trees or is_hgb_profile or is_drift_adaptive) else (7 if is_hybrid else _seasonal_lag_days(model)))
        if store.accepted_receipt_exists(cycle["cycle_id"], model["model_id"]):
            store.finish_run(run_id, "succeeded")
            print(f"cycle: {cycle['cycle_id']} already submitted")
            return "already_submitted"

        stage = "inference"
        station_ids = sorted({target["station_id"] for target in cycle["targets"]})
        history = store.history(
            station_ids,
            cycle["data_cutoff"],
            points=5000 if (is_extra_trees or is_hgb_profile or is_adaptive_profile or is_drift_adaptive) else lag_days * 96 + 96,
        )
        history_points = {
            station_id: sum(1 for row in history if row["station_id"] == station_id)
            for station_id in station_ids
        }
        print(
            "history: "
            f"stations={len(history_points)} "
            f"min_points={min(history_points.values())} "
            f"max_points={max(history_points.values())} "
            f"cutoff={cycle['data_cutoff']}"
        )
        if is_drift_horizon:
            predictions = drift_adaptive_horizon_predictions(history, cycle)
        elif is_drift_adaptive:
            predictions = drift_adaptive_predictions(history, cycle)
        elif is_adaptive_profile:
            predictions = adaptive_profile_predictions(history, cycle)
        elif is_extra_trees or is_hgb_profile:
            try:
                predictions = (
                    hgb_profile_predictions(history, cycle)
                    if is_hgb_profile
                    else extra_trees_predictions(history, cycle)
                )
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
                history = [*historical.to_dict(orient="records"), *history]
                predictions = (
                    hgb_profile_predictions(history, cycle)
                    if is_hgb_profile
                    else extra_trees_predictions(history, cycle)
                )
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

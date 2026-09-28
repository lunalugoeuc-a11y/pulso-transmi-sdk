from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pandas as pd

from pulso_transmi.client import PulsoTransmiClient
from pulso_transmi.pipeline import (
    _timestamp,
    adaptive_profile_predictions,
    extra_trees_predictions,
    hgb_profile_predictions,
    hybrid_seasonal_predictions,
)
from pulso_transmi.store import SupabaseStore


PredictionFunction = Callable[
    [list[dict[str, Any]], dict[str, Any]], list[dict[str, Any]]
]
CANDIDATES: dict[str, PredictionFunction] = {
    "adaptive_profile": adaptive_profile_predictions,
    "hgb_stack": hgb_profile_predictions,
    "extra_trees": extra_trees_predictions,
    "daily_weekly": hybrid_seasonal_predictions,
}


def model_key(algorithm: str) -> str | None:
    value = algorithm.lower()
    if "adaptive profile" in value and "hl14" in value:
        return "adaptive_profile"
    if "hgb" in value and "profile" in value:
        return "hgb_stack"
    if "extra trees" in value:
        return "extra_trees"
    if "hybrid" in value and "lag 96" in value and "lag 672" in value:
        return "daily_weekly"
    return None


def score_window(
    predictions: list[dict[str, Any]],
    observed: dict[tuple[str, str], float],
) -> dict[str, float | int]:
    by_station: dict[str, list[tuple[float, float]]] = {}
    for prediction in predictions:
        key = (str(prediction["station_id"]), str(prediction["target_at"]))
        if key not in observed:
            continue
        by_station.setdefault(key[0], []).append(
            (float(prediction["value"]), float(observed[key]))
        )
    accuracies = []
    for rows in by_station.values():
        denominator = sum(actual for _, actual in rows)
        if denominator <= 0:
            continue
        error = sum(abs(actual - predicted) for predicted, actual in rows)
        accuracies.append(max(0.0, 1.0 - error / denominator))
    if not accuracies:
        raise RuntimeError("candidate window has no evaluable stations")
    return {
        "evaluated_targets": sum(len(rows) for rows in by_station.values()),
        "station_count": len(accuracies),
        "accuracy": sum(accuracies) / len(accuracies),
        "min_station_accuracy": min(accuracies),
    }


def choose_candidate(
    scores: dict[str, dict[str, float | int]],
    active_key: str,
    *,
    minimum_improvement: float = 0.005,
    maximum_station_regression: float = 0.03,
) -> str:
    active = scores[active_key]
    best_key = max(scores, key=lambda key: float(scores[key]["accuracy"]))
    best = scores[best_key]
    if best_key == active_key:
        return active_key
    if float(best["accuracy"]) - float(active["accuracy"]) < minimum_improvement:
        return active_key
    if float(best["min_station_accuracy"]) < (
        float(active["min_station_accuracy"]) - maximum_station_regression
    ):
        return active_key
    return best_key


def _history_rows(frame: pd.DataFrame, cutoff: str) -> list[dict[str, Any]]:
    bounded = frame.loc[frame["observed_at"] <= pd.Timestamp(cutoff)].copy()
    bounded = bounded.sort_values(["station_id", "observed_at"])
    bounded = bounded.groupby("station_id", observed=True).tail(5000)
    return [
        {
            "station_id": str(row.station_id),
            "observed_at": row.observed_at.isoformat(),
            "demand": float(row.demand),
        }
        for row in bounded.itertuples(index=False)
    ]


def run_selection(api: PulsoTransmiClient, store: SupabaseStore) -> str:
    models = store.models()
    active = next((model for model in models if model["is_active"]), None)
    if active is None:
        raise RuntimeError("there is no active model")
    active_key = model_key(active["algorithm"])
    if active_key not in CANDIDATES:
        raise RuntimeError(f"unsupported active model {active['algorithm']}")

    # models() is ordered newest-first. Keep the newest registered version for
    # each implementation instead of accidentally overwriting it with an old
    # rollback entry.
    model_by_key: dict[str, dict[str, Any]] = {}
    for model in models:
        key = model_key(model["algorithm"])
        if key in CANDIDATES:
            model_by_key.setdefault(key, model)
    candidate_functions = {
        key: CANDIDATES[key] for key in model_by_key if key in CANDIDATES
    }
    if active_key not in candidate_functions:
        raise RuntimeError("active model is missing from candidate registry")

    stored_cycles = store.recent_cycles(limit=24)
    if not stored_cycles:
        raise RuntimeError("there are no stored official cycles")
    cycles = [row["contract_json"] for row in stored_cycles]
    earliest_cutoff = min(_timestamp(cycle["data_cutoff"]) for cycle in cycles)
    latest_target = max(
        _timestamp(target["target_at"])
        for cycle in cycles
        for target in cycle["targets"]
    )
    observations = api.observations_dataframe(
        start=(earliest_cutoff - timedelta(days=60)).isoformat(),
        end=latest_target.isoformat(),
        page_size=5000,
    )
    if observations.empty:
        raise RuntimeError("historical observations are empty")
    observations["station_id"] = observations["station_id"].astype(str)
    observations["demand"] = pd.to_numeric(observations["demand"], errors="coerce")
    observations = observations.dropna(subset=["demand"])
    observed_lookup = {
        (str(row.station_id), row.observed_at.isoformat()): float(row.demand)
        for row in observations.itertuples(index=False)
    }

    complete_cycles: list[dict[str, Any]] = []
    for cycle in cycles:
        target_keys = {
            (str(target["station_id"]), pd.Timestamp(target["target_at"]).isoformat())
            for target in cycle["targets"]
        }
        if all(key in observed_lookup for key in target_keys):
            complete_cycles.append(cycle)
        if len(complete_cycles) == 6:
            break
    if len(complete_cycles) < 6:
        raise RuntimeError(
            f"only {len(complete_cycles)} complete cycles are available; six required"
        )

    expected_targets = sum(cycle["expected_predictions"] for cycle in complete_cycles)
    scores: dict[str, dict[str, float | int]] = {}
    for key, predict in candidate_functions.items():
        candidate_predictions: list[dict[str, Any]] = []
        candidate_observed: dict[tuple[str, str], float] = {}
        for cycle in complete_cycles:
            history = _history_rows(observations, cycle["data_cutoff"])
            predicted = predict(history, cycle)
            candidate_predictions.extend(predicted)
            for target in cycle["targets"]:
                normalized = pd.Timestamp(target["target_at"]).isoformat()
                lookup_key = (str(target["station_id"]), normalized)
                candidate_observed[lookup_key] = observed_lookup[lookup_key]
            for prediction in predicted:
                prediction["target_at"] = pd.Timestamp(
                    prediction["target_at"]
                ).isoformat()
        metrics = score_window(candidate_predictions, candidate_observed)
        if metrics["evaluated_targets"] != expected_targets:
            raise RuntimeError(
                f"{key} coverage mismatch: {metrics['evaluated_targets']}/{expected_targets}"
            )
        scores[key] = metrics
        print(
            "candidate: "
            f"name={key} coverage={metrics['evaluated_targets']}/{expected_targets} "
            f"accuracy={100 * float(metrics['accuracy']):.2f}% "
            f"min_station={100 * float(metrics['min_station_accuracy']):.2f}%"
        )

    selected_key = choose_candidate(scores, active_key)
    if selected_key == active_key:
        print(
            f"selection: keep={active['version']} reason=guardrails "
            f"window_cycles={len(complete_cycles)}"
        )
        return "kept"

    selected_model = model_by_key[selected_key]
    promoted = store.promote_model(selected_model["model_id"])
    print(
        f"selection: promoted={promoted['version']} candidate={selected_key} "
        f"previous={active['version']} window_cycles={len(complete_cycles)}"
    )
    return "promoted"


def main() -> None:
    with PulsoTransmiClient() as api, SupabaseStore() as store:
        run_selection(api, store)


if __name__ == "__main__":
    main()

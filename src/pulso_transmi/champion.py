from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

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


def publish_summary(message: str) -> None:
    print(message)
    summary_path = os.getenv("SELECTION_SUMMARY_PATH")
    if summary_path:
        Path(summary_path).write_text(message, encoding="utf-8")


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
    observed: dict[tuple[str, datetime], float],
) -> dict[str, float | int]:
    by_station: dict[str, list[tuple[float, float]]] = {}
    for prediction in predictions:
        key = (
            str(prediction["station_id"]),
            _timestamp(str(prediction["target_at"])),
        )
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


def run_selection(api: PulsoTransmiClient, store: SupabaseStore) -> str:
    # Keep the API dependency in the public entry point, while sourcing the
    # comparison history from the same Supabase path used by production.
    del api
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
    # Ground truth belongs to the official cycle, not to the champion that
    # happened to submit it. Reading across models keeps selection operational
    # immediately after a promotion, before the new champion has six evaluated
    # submissions of its own.
    evaluated_rows = store.official_prediction_errors()
    evaluated_by_cycle: dict[
        str, dict[tuple[str, datetime], float]
    ] = {}
    for row in evaluated_rows:
        evaluated_by_cycle.setdefault(str(row["cycle_id"]), {})[
            (str(row["station_id"]), _timestamp(str(row["target_at"])))
        ] = float(row["observed_demand"])

    complete_cycles: list[dict[str, Any]] = []
    for cycle in cycles:
        target_keys = {
            (str(target["station_id"]), _timestamp(target["target_at"]))
            for target in cycle["targets"]
        }
        cycle_observed = evaluated_by_cycle.get(str(cycle["cycle_id"]), {})
        if len(cycle_observed) == int(cycle["expected_predictions"]) and all(
            key in cycle_observed for key in target_keys
        ):
            complete_cycles.append(cycle)
        if len(complete_cycles) == 6:
            break
    if len(complete_cycles) < 6:
        raise RuntimeError(
            f"only {len(complete_cycles)} complete cycles are available; six required"
        )

    history_by_cycle: dict[str, list[dict[str, Any]]] = {}
    for cycle in complete_cycles:
        station_ids = sorted(
            {str(target["station_id"]) for target in cycle["targets"]}
        )
        history_by_cycle[str(cycle["cycle_id"])] = store.history(
            station_ids, cycle["data_cutoff"], points=5000
        )

    expected_targets = sum(cycle["expected_predictions"] for cycle in complete_cycles)
    scores: dict[str, dict[str, float | int]] = {}
    for key, predict in candidate_functions.items():
        candidate_predictions: list[dict[str, Any]] = []
        candidate_observed: dict[tuple[str, datetime], float] = {}
        try:
            for cycle in complete_cycles:
                history = history_by_cycle[str(cycle["cycle_id"])]
                predicted = predict(history, cycle)
                candidate_predictions.extend(predicted)
                for target in cycle["targets"]:
                    normalized = _timestamp(target["target_at"])
                    lookup_key = (str(target["station_id"]), normalized)
                    candidate_observed[lookup_key] = evaluated_by_cycle[
                        str(cycle["cycle_id"])
                    ][lookup_key]
        except RuntimeError as exc:
            if key == active_key:
                raise
            print(f"candidate: name={key} unavailable={exc}")
            continue
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
    active_accuracy = 100 * float(scores[active_key]["accuracy"])
    best_key = max(scores, key=lambda key: float(scores[key]["accuracy"]))
    best_accuracy = 100 * float(scores[best_key]["accuracy"])
    if selected_key == active_key:
        publish_summary(
            f"selection: keep={active['version']} reason=guardrails "
            f"accuracy={active_accuracy:.2f}% best={best_key}:{best_accuracy:.2f}%"
        )
        return "kept"

    selected_model = model_by_key[selected_key]
    promoted = store.promote_model(selected_model["model_id"])
    publish_summary(
        f"selection: promoted={promoted['version']} candidate={selected_key} "
        f"accuracy={100 * float(scores[selected_key]['accuracy']):.2f}% "
        f"previous={active['version']}:{active_accuracy:.2f}%"
    )
    return "promoted"


def main() -> None:
    with PulsoTransmiClient() as api, SupabaseStore() as store:
        run_selection(api, store)


if __name__ == "__main__":
    main()

from pulso_transmi.champion import choose_candidate, model_key, score_window


def test_score_window_matches_station_wape_definition() -> None:
    predictions = [
        {"station_id": "A", "target_at": "2026-09-28T00:15:00-05:00", "value": 90},
        {"station_id": "A", "target_at": "2026-09-28T00:30:00-05:00", "value": 110},
        {"station_id": "B", "target_at": "2026-09-28T00:15:00-05:00", "value": 50},
        {"station_id": "B", "target_at": "2026-09-28T00:30:00-05:00", "value": 50},
    ]
    observed = {
        ("A", _utc("2026-09-28T05:15:00Z")): 100,
        ("A", _utc("2026-09-28T05:30:00Z")): 100,
        ("B", _utc("2026-09-28T05:15:00Z")): 100,
        ("B", _utc("2026-09-28T05:30:00Z")): 100,
    }

    score = score_window(predictions, observed)

    assert score["evaluated_targets"] == 4
    assert score["station_count"] == 2
    assert score["accuracy"] == 0.7
    assert score["min_station_accuracy"] == 0.5


def _utc(value: str):
    from pulso_transmi.pipeline import _timestamp

    return _timestamp(value)


def test_selection_requires_material_gain_and_station_guardrail() -> None:
    scores = {
        "adaptive_profile": {"accuracy": 0.70, "min_station_accuracy": 0.50},
        "hgb_stack": {"accuracy": 0.72, "min_station_accuracy": 0.48},
    }
    assert choose_candidate(scores, "adaptive_profile") == "hgb_stack"

    scores["hgb_stack"] = {"accuracy": 0.704, "min_station_accuracy": 0.60}
    assert choose_candidate(scores, "adaptive_profile") == "adaptive_profile"

    scores["hgb_stack"] = {"accuracy": 0.75, "min_station_accuracy": 0.40}
    assert choose_candidate(scores, "adaptive_profile") == "adaptive_profile"


def test_model_algorithm_mapping() -> None:
    assert model_key("Adaptive profile HL14") == "adaptive_profile"
    assert (
        model_key("Drift adaptive horizon Extra Trees")
        == "drift_adaptive_horizon"
    )
    assert model_key("Drift adaptive Extra Trees") == "drift_adaptive"
    assert model_key("Stacked HGB + profile") == "hgb_stack"
    assert model_key("Extra Trees") == "extra_trees"
    assert model_key("Hybrid lag 96 lag 672") == "daily_weekly"

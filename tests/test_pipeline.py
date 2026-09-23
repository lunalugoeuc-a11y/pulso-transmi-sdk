from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pulso_transmi.pipeline import (
    run_pipeline,
    hybrid_seasonal_predictions,
    seasonal_naive_predictions,
    validate_exact_targets,
)


def cycle() -> dict[str, Any]:
    origin = datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc)
    targets = [
        {
            "station_id": station,
            "target_at": (origin + timedelta(minutes=horizon)).isoformat(),
            "horizon_minutes": horizon,
        }
        for station in ("02300", "03000")
        for horizon in (15, 30)
    ]
    return {
        "cycle_id": "cyc_test_20990101T100000Z",
        "state": "open",
        "origin_at": origin.isoformat(),
        "data_cutoff": origin.isoformat(),
        "opens_at": origin.isoformat(),
        "closes_at": (origin + timedelta(minutes=25)).isoformat(),
        "forecast_start_at": targets[0]["target_at"],
        "forecast_end_at": targets[-1]["target_at"],
        "station_count": 2,
        "horizons_minutes": [15, 30],
        "expected_predictions": 4,
        "targets": targets,
    }


class FakeApi:
    def __init__(self, current: dict[str, Any] | None) -> None:
        self.current = current
        self.submitted: dict[str, Any] | None = None

    def stream_observations_page(self, **_: Any) -> dict[str, Any]:
        return {"data": [], "next_cursor": None}

    def current_cycle(self) -> dict[str, Any] | None:
        return self.current

    def submit(self, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        self.submitted = payload
        assert idempotency_key.startswith("ptm-")
        return {
            "submission_id": "sub_test",
            "status": "accepted",
            "attempt": 1,
            "received_at": "2099-01-01T10:01:00Z",
            "closes_at": "2099-01-01T10:25:00Z",
            "predictions_received": 4,
            "expected_predictions": 4,
            "validated_contract": {},
            "is_official": True,
        }


class FakeStore:
    def __init__(self, *, already_submitted: bool = False) -> None:
        self.already_submitted = already_submitted
        self.finished: list[str] = []
        self.saved_receipt = False

    def create_run(self, *_: Any) -> str:
        return "00000000-0000-0000-0000-000000000001"

    def collector_cursor(self) -> None:
        return None

    def ingest_observation_page(self, *_: Any) -> int:
        return 0

    def finish_run(self, _run_id: str, status: str) -> None:
        self.finished.append(status)

    def save_cycle(self, _cycle: dict[str, Any]) -> None:
        pass

    def active_model(self) -> dict[str, Any]:
        return {
            "model_id": "00000000-0000-0000-0000-000000000002",
            "algorithm": "Seasonal naive lag 96",
            "version": "1.0.0",
            "created_at": "2098-12-01T00:00:00Z",
            "training_runs": {"train_end": "2098-12-01T00:00:00Z"},
        }

    def accepted_receipt_exists(self, *_: Any) -> bool:
        return self.already_submitted

    def history(self, station_ids: list[str], *_: Any, **__: Any) -> list[dict[str, Any]]:
        cutoff = datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc)
        return [
            {
                "station_id": station,
                "observed_at": (cutoff - timedelta(days=1) + timedelta(minutes=offset)).isoformat(),
                "demand": 100 + offset,
            }
            for station in station_ids
            for offset in (15, 30)
        ]

    def save_predictions(self, **kwargs: Any) -> list[str]:
        assert len(kwargs["predictions"]) == 4
        return [f"prediction-{index}" for index in range(4)]

    def save_receipt(self, **_: Any) -> str:
        self.saved_receipt = True
        return "local-submission"

    def record_error(self, *_: Any, **__: Any) -> None:
        pass


def test_no_cycle_finishes_successfully(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    store = FakeStore()
    result = run_pipeline(FakeApi(None), store)
    assert result == "no_open_cycle"
    assert store.finished == ["succeeded"]


def test_pipeline_submits_exact_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    api = FakeApi(cycle())
    store = FakeStore()
    result = run_pipeline(api, store)
    assert result == "submitted"
    assert api.submitted is not None
    assert {(p["station_id"], p["target_at"]) for p in api.submitted["predictions"]} == {
        (target["station_id"], target["target_at"]) for target in cycle()["targets"]
    }
    assert store.saved_receipt is True


def test_exact_target_validation_rejects_duplicates() -> None:
    current = cycle()
    prediction = {
        "station_id": current["targets"][0]["station_id"],
        "target_at": current["targets"][0]["target_at"],
        "value": 10.0,
    }
    with pytest.raises(ValueError, match="duplicate"):
        validate_exact_targets([prediction, prediction], current)


def test_weekly_seasonal_predictions_use_seven_day_lag() -> None:
    current = cycle()
    history = [
        {
            "station_id": target["station_id"],
            "observed_at": (
                datetime.fromisoformat(target["target_at"]) - timedelta(days=7)
            ).isoformat(),
            "demand": 321,
        }
        for target in current["targets"]
    ]
    predictions = seasonal_naive_predictions(history, current, lag_days=7)
    assert len(predictions) == current["expected_predictions"]
    assert {prediction["value"] for prediction in predictions} == {321}


def test_hybrid_predictions_average_daily_and_weekly_lags() -> None:
    current = cycle()
    history = []
    for target in current["targets"]:
        target_at = datetime.fromisoformat(target["target_at"])
        history.extend(
            [
                {
                    "station_id": target["station_id"],
                    "observed_at": (target_at - timedelta(days=1)).isoformat(),
                    "demand": 100,
                },
                {
                    "station_id": target["station_id"],
                    "observed_at": (target_at - timedelta(days=7)).isoformat(),
                    "demand": 300,
                },
            ]
        )
    predictions = hybrid_seasonal_predictions(history, current)
    assert len(predictions) == current["expected_predictions"]
    assert {prediction["value"] for prediction in predictions} == {200}

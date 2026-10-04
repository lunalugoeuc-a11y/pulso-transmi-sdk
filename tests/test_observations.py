import pytest

from pulso_transmi.observations import (
    ObservationContractError,
    normalize_stream_observation,
    normalize_stream_page,
    stream_source_version,
)


def test_normalizes_mixed_v1_v2_page() -> None:
    rows = normalize_stream_page(
        [
            {
                "station_id": "02300",
                "observed_at": "2026-09-20T12:00:00Z",
                "released_at": "2026-09-20T12:15:00Z",
                "demand": 123,
            },
            {
                "schema_version": 2,
                "station_id": "02300",
                "observed_at": "2026-09-20T12:15:00Z",
                "released_at": "2026-09-20T12:30:00Z",
                "measurement": {
                    "value": "341.00",
                    "unit": "passengers",
                    "quality": "observed",
                },
            },
        ]
    )

    assert [row["demand"] for row in rows] == [123, 341.0]
    assert [row["source_schema_version"] for row in rows] == [1, 2]
    assert stream_source_version(rows) == "competition-stream-v1-v2"


def test_missing_v2_is_preserved_as_missing_not_zero() -> None:
    row = normalize_stream_observation(
        {
            "schema_version": 2,
            "station_id": "05100",
            "observed_at": "2026-09-20T12:15:00+00:00",
            "released_at": "2026-09-20T12:30:00+00:00",
            "measurement": {
                "value": None,
                "unit": "passengers",
                "quality": "missing",
            },
        }
    )

    assert row["quality"] == "missing"
    assert row["demand"] is None


@pytest.mark.parametrize(
    "measurement",
    [
        {"value": "1,000", "unit": "passengers", "quality": "observed"},
        {"value": 1000, "unit": "passengers", "quality": "observed"},
        {"value": "10", "unit": "people", "quality": "observed"},
        {"value": "10", "unit": "passengers", "quality": "estimated"},
        {"value": "10", "unit": "passengers", "quality": "missing"},
    ],
)
def test_rejects_invalid_v2_measurement(measurement: dict[str, object]) -> None:
    with pytest.raises(ObservationContractError):
        normalize_stream_observation(
            {
                "schema_version": 2,
                "station_id": "02300",
                "observed_at": "2026-09-20T12:15:00Z",
                "released_at": "2026-09-20T12:30:00Z",
                "measurement": measurement,
            }
        )


def test_rejects_timezone_free_timestamp() -> None:
    with pytest.raises(ObservationContractError, match="timezone"):
        normalize_stream_observation(
            {
                "station_id": "02300",
                "observed_at": "2026-09-20T12:00:00",
                "released_at": "2026-09-20T12:15:00Z",
                "demand": 3,
            }
        )

from urllib.parse import parse_qs

import httpx

from pulso_transmi.store import SupabaseStore


def test_opaque_secret_uses_apikey_without_bearer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == "sb_secret_test"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=[{"cursor_value": "cursor-1"}])

    with SupabaseStore(
        "https://project.supabase.co",
        "sb_secret_test",
        transport=httpx.MockTransport(handler),
    ) as store:
        assert store.collector_cursor() == "cursor-1"


def test_ingestion_uses_single_rpc_call() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=2)

    with SupabaseStore(
        "https://project.supabase.co",
        "header.payload.signature",
        transport=httpx.MockTransport(handler),
    ) as store:
        count = store.ingest_observation_page(
            "run-id",
            "cursor-2",
            [
                {"station_id": "02300", "observed_at": "2026-09-22T10:00:00Z", "demand": 3},
                {"station_id": "02300", "observed_at": "2026-09-22T10:15:00Z", "demand": 4},
            ],
        )

    assert count == 2
    assert len(requests) == 1
    assert requests[0].url.path == "/rest/v1/rpc/ingest_observation_page"
    assert requests[0].headers["authorization"] == "Bearer header.payload.signature"


def test_evaluation_uses_backend_rpc() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=48)

    with SupabaseStore(
        "https://project.supabase.co",
        "sb_secret_test",
        transport=httpx.MockTransport(handler),
    ) as store:
        assert store.evaluate_available_predictions() == 48

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/rest/v1/rpc/evaluate_available_predictions"


def test_official_prediction_errors_are_filtered_by_model() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "cycle_id": "cycle-1",
                    "station_id": "05100",
                    "target_at": "2026-09-28T05:15:00Z",
                    "observed_demand": 42,
                }
            ],
        )

    with SupabaseStore(
        "https://project.supabase.co",
        "sb_secret_test",
        transport=httpx.MockTransport(handler),
    ) as store:
        rows = store.official_prediction_errors("model-6", limit=288)

    assert rows[0]["observed_demand"] == 42
    query = parse_qs(requests[0].url.query.decode())
    assert requests[0].url.path == "/rest/v1/official_prediction_errors"
    assert query["model_id"] == ["eq.model-6"]
    assert query["limit"] == ["288"]


def test_history_paginates_past_supabase_row_cap() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        query = parse_qs(request.url.query.decode())
        offset = int(query["offset"][0])
        limit = int(query["limit"][0])
        available = max(0, 1500 - offset)
        count = min(limit, available)
        return httpx.Response(
            200,
            json=[
                {
                    "station_id": "02300",
                    "observed_at": f"2026-09-01T00:{index % 60:02d}:00Z",
                    "demand": index,
                }
                for index in range(offset, offset + count)
            ],
        )

    with SupabaseStore(
        "https://project.supabase.co",
        "sb_secret_test",
        transport=httpx.MockTransport(handler),
    ) as store:
        rows = store.history(["02300"], "2026-09-27T00:00:00Z", points=1500)

    assert len(rows) == 1500
    assert len(requests) == 2
    assert [parse_qs(request.url.query.decode())["offset"][0] for request in requests] == [
        "0",
        "1000",
    ]


def test_history_stops_when_station_history_is_exhausted() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        query = parse_qs(request.url.query.decode())
        offset = int(query["offset"][0])
        count = max(0, min(250, 1250 - offset))
        return httpx.Response(
            200,
            json=[
                {
                    "station_id": "02300",
                    "observed_at": f"2026-09-01T00:{index % 60:02d}:00Z",
                    "demand": index,
                }
                for index in range(offset, offset + count)
            ],
        )

    with SupabaseStore(
        "https://project.supabase.co",
        "sb_secret_test",
        transport=httpx.MockTransport(handler),
    ) as store:
        rows = store.history(["02300"], "2026-09-27T00:00:00Z", points=5000)

    assert len(rows) == 250
    assert len(requests) == 1

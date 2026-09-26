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

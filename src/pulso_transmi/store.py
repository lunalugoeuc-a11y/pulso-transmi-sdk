from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx


class SupabaseStoreError(RuntimeError):
    """Raised when operational state cannot be persisted."""


class SupabaseStore:
    """Small PostgREST client restricted to backend service credentials."""

    def __init__(
        self,
        url: str | None = None,
        service_key: str | None = None,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        resolved_url = (url or os.getenv("SUPABASE_URL", "")).rstrip("/")
        resolved_key = service_key or os.getenv("SUPABASE_SERVICE_KEY", "")
        if not resolved_url or not resolved_key:
            raise SupabaseStoreError(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY are required"
            )
        headers = {"apikey": resolved_key, "Content-Type": "application/json"}
        # Legacy service-role keys are JWTs. New sb_secret keys must only use
        # apikey; Supabase's gateway supplies their internal authorization.
        if resolved_key.count(".") == 2:
            headers["Authorization"] = f"Bearer {resolved_key}"
        self._client = httpx.Client(
            base_url=f"{resolved_url}/rest/v1",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> "SupabaseStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._client.request(method, path, **kwargs)
        if response.is_error:
            try:
                detail = response.json()
            except ValueError:
                detail = {"message": response.text[:300]}
            raise SupabaseStoreError(
                f"Supabase {method} {path} failed ({response.status_code}): {detail}"
            )
        return response

    def create_run(self, run_type: str, commit_sha: str | None, trigger: str) -> str:
        response = self._request(
            "POST",
            "/pipeline_runs",
            headers={"Prefer": "return=representation"},
            json={
                "run_type": run_type,
                "status": "running",
                "commit_sha": commit_sha,
                "trigger_reason": trigger,
            },
        )
        return response.json()[0]["pipeline_run_id"]

    def finish_run(self, run_id: str, status: str) -> None:
        self._request(
            "PATCH",
            "/pipeline_runs",
            params={"pipeline_run_id": f"eq.{run_id}"},
            json={"status": status, "finished_at": datetime.now(timezone.utc).isoformat()},
        )

    def record_error(
        self, run_id: str, stage: str, error: BaseException, *, retryable: bool
    ) -> None:
        self._request(
            "POST",
            "/pipeline_errors",
            json={
                "pipeline_run_id": run_id,
                "stage": stage,
                "error_type": type(error).__name__,
                "error_message": str(error)[:2000],
                "retryable": retryable,
            },
        )

    def collector_cursor(self) -> str | None:
        response = self._request(
            "GET",
            "/collector_state",
            params={
                "collector_name": "eq.observations",
                "select": "cursor_value",
                "limit": 1,
            },
        )
        rows = response.json()
        return rows[0]["cursor_value"] if rows else None

    def ingest_observation_page(
        self, run_id: str, cursor: str | None, rows: list[dict[str, Any]]
    ) -> int:
        response = self._request(
            "POST",
            "/rpc/ingest_observation_page",
            json={
                "p_pipeline_run_id": run_id,
                "p_cursor": cursor,
                "p_rows": rows,
                "p_source_version": "competition-stream-v1",
            },
        )
        return int(response.json())

    def save_cycle(self, cycle: dict[str, Any]) -> None:
        row = {
            key: cycle[key]
            for key in (
                "cycle_id",
                "state",
                "origin_at",
                "data_cutoff",
                "opens_at",
                "closes_at",
                "forecast_start_at",
                "forecast_end_at",
                "station_count",
                "horizons_minutes",
                "expected_predictions",
            )
        }
        row.update(
            {
                "contract_json": cycle,
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._request(
            "POST",
            "/forecast_cycles",
            params={"on_conflict": "cycle_id"},
            headers={"Prefer": "resolution=merge-duplicates"},
            json=row,
        )

    def active_model(self) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/models",
            params={
                "is_active": "eq.true",
                "select": (
                    "model_id,model_name,algorithm,version,artifact_uri,created_at,"
                    "training_runs!inner(training_run_id,train_end)"
                ),
                "order": "created_at.desc",
                "limit": 1,
            },
        )
        rows = response.json()
        if not rows:
            raise SupabaseStoreError("there is no promoted model")
        return rows[0]

    def accepted_receipt_exists(self, cycle_id: str, model_id: str) -> bool:
        response = self._request(
            "GET",
            "/submissions",
            params={
                "cycle_id": f"eq.{cycle_id}",
                "model_id": f"eq.{model_id}",
                "status": "eq.accepted",
                "select": "submission_id",
                "limit": 1,
            },
        )
        return bool(response.json())

    def history(
        self, station_ids: list[str], cutoff: str, *, points: int = 192
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for station_id in station_ids:
            response = self._request(
                "GET",
                "/observations",
                params={
                    "station_id": f"eq.{station_id}",
                    "observed_at": f"lte.{cutoff}",
                    "select": "station_id,observed_at,demand",
                    "order": "observed_at.desc",
                    "limit": points,
                },
            )
            rows.extend(response.json())
        return rows

    def save_predictions(
        self,
        *,
        run_id: str,
        model_id: str,
        cycle_id: str,
        predictions: list[dict[str, Any]],
        targets: list[dict[str, Any]],
    ) -> list[str]:
        target_by_pair = {
            (item["station_id"], item["target_at"]): item for item in targets
        }
        rows = []
        for prediction in predictions:
            target = target_by_pair[(prediction["station_id"], prediction["target_at"])]
            horizon_minutes = int(target["horizon_minutes"])
            rows.append(
                {
                    "pipeline_run_id": run_id,
                    "model_id": model_id,
                    "station_id": prediction["station_id"],
                    "target_at": prediction["target_at"],
                    "horizon_steps": horizon_minutes // 15,
                    "horizon_minutes": horizon_minutes,
                    "predicted_demand": prediction["value"],
                    "cycle_id": cycle_id,
                }
            )
        response = self._request(
            "POST",
            "/predictions",
            params={"on_conflict": "model_id,station_id,target_at,horizon_steps"},
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            json=rows,
        )
        return [row["prediction_id"] for row in response.json()]

    def save_receipt(
        self,
        *,
        run_id: str,
        model_id: str,
        commit_sha: str,
        cycle_id: str,
        client_run_id: str,
        idempotency_key: str,
        payload_hash: str,
        receipt: dict[str, Any],
        prediction_ids: list[str],
    ) -> str:
        row = {
            "pipeline_run_id": run_id,
            "model_id": model_id,
            "commit_sha": commit_sha,
            "status": receipt["status"],
            "response_message": "accepted by central API",
            "cycle_id": cycle_id,
            "central_submission_id": receipt["submission_id"],
            "client_run_id": client_run_id,
            "idempotency_key": idempotency_key,
            "payload_hash": payload_hash,
            "attempt": receipt.get("attempt"),
            "received_at": receipt.get("received_at"),
            "closes_at": receipt.get("closes_at"),
            "predictions_received": receipt.get("predictions_received"),
            "expected_predictions": receipt.get("expected_predictions"),
            "validated_contract": receipt.get("validated_contract"),
            "receipt_json": receipt,
            "request_id": receipt.get("request_id"),
            "is_official": receipt.get("is_official", False),
            "replaced_submission_id": receipt.get("replaced_submission_id"),
        }
        response = self._request(
            "POST",
            "/submissions",
            params={"on_conflict": "central_submission_id"},
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            json=row,
        )
        submission_id = response.json()[0]["submission_id"]
        if prediction_ids:
            self._request(
                "POST",
                "/submission_predictions",
                params={"on_conflict": "submission_id,prediction_id"},
                headers={"Prefer": "resolution=ignore-duplicates"},
                json=[
                    {"submission_id": submission_id, "prediction_id": prediction_id}
                    for prediction_id in prediction_ids
                ],
            )
        return submission_id

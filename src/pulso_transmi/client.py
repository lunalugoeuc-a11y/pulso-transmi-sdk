from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
import pandas as pd


DEFAULT_BASE_URL = "https://pulso-transmi.72-60-245-2.sslip.io"


class PulsoTransmiError(RuntimeError):
    """Raised when the Pulso TransMi API cannot fulfill a request."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id


class PulsoTransmiClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        resolved_url = base_url or os.getenv("PULSO_API_URL", DEFAULT_BASE_URL)
        resolved_key = api_key or os.getenv("PULSO_API_KEY")
        headers = {"User-Agent": "pulso-transmi-python/0.1.0"}
        if resolved_key:
            headers["Authorization"] = f"Bearer {resolved_key}"
        self._client = httpx.Client(
            base_url=resolved_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )

    def __enter__(self) -> "PulsoTransmiClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise PulsoTransmiError(f"GET {path} failed: {exc}") from exc

    @staticmethod
    def _api_error(response: httpx.Response, operation: str) -> PulsoTransmiError:
        try:
            detail = response.json().get("detail", {})
        except (json.JSONDecodeError, AttributeError, TypeError):
            detail = {}
        code = detail.get("code") if isinstance(detail, dict) else None
        message = detail.get("message") if isinstance(detail, dict) else None
        request_id = response.headers.get("X-Request-ID")
        return PulsoTransmiError(
            message or f"{operation} failed with HTTP {response.status_code}",
            status_code=response.status_code,
            code=code,
            request_id=request_id,
        )

    def meta(self) -> dict[str, Any]:
        return self._get("/v1/meta").json()

    def stations(self) -> pd.DataFrame:
        payload = self._get("/v1/stations").json()
        frame = pd.DataFrame(payload["data"])
        if not frame.empty:
            frame["station_id"] = frame["station_id"].astype("string")
        return frame

    def observations_page(
        self,
        *,
        station_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
        cursor: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        params = {
            "station_id": station_id,
            "start": start,
            "end": end,
            "cursor": cursor,
            "limit": limit,
        }
        return self._get("/v1/observations", params={key: value for key, value in params.items() if value is not None}).json()

    def context_page(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        cursor: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        params = {"start": start, "end": end, "cursor": cursor, "limit": limit}
        return self._get("/v1/context", params={key: value for key, value in params.items() if value is not None}).json()

    def _all_pages(self, endpoint: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        cursor = None
        seen: set[str] = set()
        while True:
            page_params = {**params, "cursor": cursor}
            payload = self._get(endpoint, params={key: value for key, value in page_params.items() if value is not None}).json()
            yield from payload["data"]
            cursor = payload.get("next_cursor")
            if cursor is None:
                break
            if cursor in seen:
                raise PulsoTransmiError("API returned a repeated cursor")
            seen.add(cursor)

    def observations_dataframe(
        self,
        *,
        station_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
        page_size: int = 5000,
    ) -> pd.DataFrame:
        rows = self._all_pages(
            "/v1/observations",
            {"station_id": station_id, "start": start, "end": end, "limit": page_size},
        )
        frame = pd.DataFrame(rows)
        if not frame.empty:
            frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
            frame["station_id"] = frame["station_id"].astype("string")
        return frame

    def context_dataframe(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        page_size: int = 5000,
    ) -> pd.DataFrame:
        rows = self._all_pages(
            "/v1/context", {"start": start, "end": end, "limit": page_size}
        )
        frame = pd.DataFrame(rows)
        if not frame.empty:
            frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
        return frame

    def stream_observations_page(
        self, *, cursor: str | None = None, limit: int = 5000
    ) -> dict[str, Any]:
        """Return one competition stream page without interpreting its cursor."""
        response = self._client.get(
            "/v1/stream/observations",
            params={key: value for key, value in {"cursor": cursor, "limit": limit}.items() if value is not None},
        )
        if response.is_error:
            raise self._api_error(response, "stream observations")
        return response.json()

    def current_cycle(self) -> dict[str, Any] | None:
        """Return the open cycle, or ``None`` for the expected no-cycle response."""
        response = self._client.get("/v1/forecast-cycles/current")
        if response.status_code == 404:
            error = self._api_error(response, "current cycle")
            if error.code == "no_open_cycle":
                return None
            raise error
        if response.is_error:
            raise self._api_error(response, "current cycle")
        return response.json()

    def identity(self) -> dict[str, Any]:
        response = self._client.get("/v1/me")
        if response.is_error:
            raise self._api_error(response, "participant identity")
        return response.json()

    def submit(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        attempts: int = 4,
    ) -> dict[str, Any]:
        """Submit with bounded backoff while preserving the idempotency key."""
        for attempt in range(attempts):
            try:
                response = self._client.post(
                    "/v1/submissions",
                    headers={"Idempotency-Key": idempotency_key},
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt + 1 == attempts:
                    raise PulsoTransmiError("submission transport failed") from exc
                time.sleep(2**attempt)
                continue
            if response.status_code in {200, 201}:
                receipt = response.json()
                receipt["request_id"] = response.headers.get("X-Request-ID")
                return receipt
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
            raise self._api_error(response, "submission")
        raise PulsoTransmiError("submission attempts exhausted")

    def download(self, filename: str, destination: str | Path) -> Path:
        allowed = {"stations.csv", "observations.csv", "context.csv", "metadata.json"}
        if filename not in allowed:
            raise ValueError(f"unsupported filename: {filename}")
        response = self._get(f"/v1/downloads/{filename}")
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)

        if filename != "metadata.json":
            expected = self.meta()["dataset"]["files"][filename]["sha256"]
            actual = hashlib.sha256(response.content).hexdigest()
            if actual != expected:
                path.unlink(missing_ok=True)
                raise PulsoTransmiError(f"checksum mismatch for {filename}")
        return path

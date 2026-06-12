from __future__ import annotations

import httpx

from secondbrain.observability import log_metadata

INTAKE_TOKEN_HEADER = "X-Second-Brain-Intake-Token"


class N8nWebhookDeliveryClient:
    """Posts minimal delivery envelopes to the n8n intake webhook."""

    def __init__(self, *, webhook_url: str, webhook_token: str, timeout_seconds: int = 10) -> None:
        self._webhook_url = webhook_url
        self._webhook_token = webhook_token
        self._timeout = timeout_seconds

    async def forward_capture(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
    ) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                self._webhook_url,
                json={"capture_id": capture_id, "delivery_attempt": delivery_attempt},
                headers={INTAKE_TOKEN_HEADER: self._webhook_token},
            )
        if response.status_code >= 500:
            log_metadata(
                "n8n_webhook_server_error",
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                status_code=response.status_code,
            )
            response.raise_for_status()
        if response.status_code >= 400:
            log_metadata(
                "n8n_webhook_client_error",
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                status_code=response.status_code,
            )
            response.raise_for_status()

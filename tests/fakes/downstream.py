from __future__ import annotations

import httpx

from secondbrain.capture_api import INTERNAL_TOKEN_HEADER


class FakeDownstreamClient:
    def __init__(self, app, *, token: str):
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self._headers = {INTERNAL_TOKEN_HEADER: token}

    async def aclose(self):
        await self._client.aclose()

    async def get_capture(self, capture_id: str):
        return await self._client.get(
            f"/internal/captures/{capture_id}",
            headers=self._headers,
        )

    async def edit_receipt(self, capture_id: str, content: str):
        return await self._client.post(
            f"/internal/receipts/{capture_id}/edit",
            headers=self._headers,
            json={"content": content},
        )

    # ------------------------------------------------------------------
    # Attempt-aware downstream delivery callbacks
    # ------------------------------------------------------------------

    async def acknowledge_forwarded(self, capture_id: str, delivery_attempt: int):
        return await self._client.post(
            f"/internal/captures/{capture_id}/delivery/acknowledge-forwarded",
            headers=self._headers,
            json={"delivery_attempt": delivery_attempt},
        )

    async def acknowledge_classifying(self, capture_id: str, delivery_attempt: int):
        return await self._client.post(
            f"/internal/captures/{capture_id}/delivery/acknowledge-classifying",
            headers=self._headers,
            json={"delivery_attempt": delivery_attempt},
        )

    async def renew_lease(self, capture_id: str, delivery_attempt: int):
        return await self._client.post(
            f"/internal/captures/{capture_id}/delivery/renew-lease",
            headers=self._headers,
            json={"delivery_attempt": delivery_attempt},
        )

    async def acknowledge_filed(
        self,
        capture_id: str,
        delivery_attempt: int,
        note_path: str,
        git_commit_hash: str | None = None,
    ):
        payload: dict = {"delivery_attempt": delivery_attempt, "note_path": note_path}
        if git_commit_hash is not None:
            payload["git_commit_hash"] = git_commit_hash
        return await self._client.post(
            f"/internal/captures/{capture_id}/delivery/acknowledge-filed",
            headers=self._headers,
            json=payload,
        )

    async def acknowledge_inbox(
        self,
        capture_id: str,
        delivery_attempt: int,
        note_path: str,
        git_commit_hash: str | None = None,
        reason_type: str = "",
    ):
        payload: dict = {
            "delivery_attempt": delivery_attempt,
            "note_path": note_path,
            "reason_type": reason_type,
        }
        if git_commit_hash is not None:
            payload["git_commit_hash"] = git_commit_hash
        return await self._client.post(
            f"/internal/captures/{capture_id}/delivery/acknowledge-inbox",
            headers=self._headers,
            json=payload,
        )

    async def schedule_retry(
        self,
        capture_id: str,
        delivery_attempt: int,
        error_type: str,
        reason_type: str = "webhook_failure",
    ):
        return await self._client.post(
            f"/internal/captures/{capture_id}/delivery/schedule-retry",
            headers=self._headers,
            json={
                "delivery_attempt": delivery_attempt,
                "error_type": error_type,
                "reason_type": reason_type,
            },
        )

    async def acknowledge_failed(
        self,
        capture_id: str,
        delivery_attempt: int,
        reason_type: str = "",
    ):
        return await self._client.post(
            f"/internal/captures/{capture_id}/delivery/acknowledge-failed",
            headers=self._headers,
            json={"delivery_attempt": delivery_attempt, "reason_type": reason_type},
        )

    async def report_workflow_error(
        self,
        capture_id: str,
        delivery_attempt: int,
        disposition: str,
        error_type: str,
        reason_type: str = "workflow_error",
        workflow_id: str = "test_workflow",
        workflow_name: str = "second_brain_intake",
        execution_id: str | None = None,
        stage: str = "workflow_unknown",
    ):
        payload: dict = {
            "delivery_attempt": delivery_attempt,
            "disposition": disposition,
            "error_type": error_type,
            "reason_type": reason_type,
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "execution_id": execution_id,
            "stage": stage,
        }
        return await self._client.post(
            f"/internal/captures/{capture_id}/delivery/report-workflow-error",
            headers=self._headers,
            json=payload,
        )

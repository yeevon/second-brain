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

    async def mark_forwarded(self, capture_id: str):
        return await self._client.post(
            f"/internal/captures/{capture_id}/mark-forwarded",
            headers=self._headers,
        )

    async def mark_classifying(self, capture_id: str):
        return await self._client.post(
            f"/internal/captures/{capture_id}/mark-classifying",
            headers=self._headers,
        )

    async def mark_filed(self, capture_id: str, payload: dict):
        return await self._client.post(
            f"/internal/captures/{capture_id}/mark-filed",
            headers=self._headers,
            json=payload,
        )

    async def mark_inbox(self, capture_id: str, payload: dict):
        return await self._client.post(
            f"/internal/captures/{capture_id}/mark-inbox",
            headers=self._headers,
            json=payload,
        )

    async def mark_failed(self, capture_id: str, reason: str):
        return await self._client.post(
            f"/internal/captures/{capture_id}/mark-failed",
            headers=self._headers,
            json={"reason": reason},
        )

    async def retry(self, capture_id: str):
        return await self._client.post(
            f"/internal/captures/{capture_id}/retry",
            headers=self._headers,
        )

    async def edit_receipt(self, capture_id: str, content: str):
        return await self._client.post(
            f"/internal/receipts/{capture_id}/edit",
            headers=self._headers,
            json={"content": content},
        )

"""Unit tests for N8n delivery client and downstream delivery config."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from secondbrain.config import Settings
from secondbrain.n8n_delivery import INTAKE_TOKEN_HEADER, N8nWebhookDeliveryClient


# ── Config helpers ────────────────────────────────────────────────────────────

_VALID_TOKEN = "a" * 32
_VALID_URL = "http://n8n:5678/webhook/second-brain-intake"

_BASE_ENV = {
    "CAPTURE_PROCESSING_MODE": "capture-only",
    "DISCORD_BOT_TOKEN": "tok",
    "DISCORD_GUILD_ID": "100",
    "DISCORD_CAPTURE_CHANNEL_ID": "200",
    "DISCORD_ALLOWED_USER_ID": "300",
    "CAPTURE_SERVICE_INTERNAL_TOKEN": "x" * 32,
    "CAPTURE_API_HOST": "127.0.0.1",
    "CAPTURE_API_PORT": "8000",
    "LEDGER_PATH": ".runtime/ledger.sqlite3",
}


def _set_base(monkeypatch):
    for k, v in _BASE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DOWNSTREAM_DELIVERY_ENABLED", raising=False)
    monkeypatch.delenv("N8N_INTAKE_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("N8N_INTAKE_WEBHOOK_TOKEN", raising=False)
    monkeypatch.delenv("DELIVERY_WEBHOOK_TIMEOUT_SECONDS", raising=False)


# ── Downstream delivery config ────────────────────────────────────────────────


def test_downstream_delivery_disabled_by_default(monkeypatch):
    _set_base(monkeypatch)
    s = Settings()
    assert s.downstream_delivery_enabled is False


def test_downstream_delivery_requires_url_when_enabled(monkeypatch):
    _set_base(monkeypatch)
    monkeypatch.setenv("DOWNSTREAM_DELIVERY_ENABLED", "true")
    # URL not set
    with pytest.raises(RuntimeError, match="N8N_INTAKE_WEBHOOK_URL is required"):
        Settings()


def test_downstream_delivery_rejects_wrong_url_prefix(monkeypatch):
    _set_base(monkeypatch)
    monkeypatch.setenv("DOWNSTREAM_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("N8N_INTAKE_WEBHOOK_URL", "http://n8n:5678/webhook/other-path")
    monkeypatch.setenv("N8N_INTAKE_WEBHOOK_TOKEN", _VALID_TOKEN)
    with pytest.raises(RuntimeError, match="N8N_INTAKE_WEBHOOK_URL must be"):
        Settings()


def test_downstream_delivery_requires_token_when_enabled(monkeypatch):
    _set_base(monkeypatch)
    monkeypatch.setenv("DOWNSTREAM_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("N8N_INTAKE_WEBHOOK_URL", _VALID_URL)
    # token not set
    with pytest.raises(RuntimeError, match="N8N_INTAKE_WEBHOOK_TOKEN is required"):
        Settings()


def test_downstream_delivery_rejects_short_token(monkeypatch):
    _set_base(monkeypatch)
    monkeypatch.setenv("DOWNSTREAM_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("N8N_INTAKE_WEBHOOK_URL", _VALID_URL)
    monkeypatch.setenv("N8N_INTAKE_WEBHOOK_TOKEN", "too-short")
    with pytest.raises(RuntimeError, match="at least 32 characters"):
        Settings()


def test_downstream_delivery_accepts_valid_config(monkeypatch):
    _set_base(monkeypatch)
    monkeypatch.setenv("DOWNSTREAM_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("N8N_INTAKE_WEBHOOK_URL", _VALID_URL)
    monkeypatch.setenv("N8N_INTAKE_WEBHOOK_TOKEN", _VALID_TOKEN)
    s = Settings()
    assert s.downstream_delivery_enabled is True
    assert s.n8n_intake_webhook_url == _VALID_URL
    assert s.n8n_intake_webhook_token == _VALID_TOKEN


def test_delivery_webhook_timeout_defaults_to_ten(monkeypatch):
    _set_base(monkeypatch)
    s = Settings()
    assert s.delivery_webhook_timeout_seconds == 10


def test_delivery_webhook_timeout_must_be_at_least_one(monkeypatch):
    _set_base(monkeypatch)
    monkeypatch.setenv("DELIVERY_WEBHOOK_TIMEOUT_SECONDS", "0")
    with pytest.raises(RuntimeError, match="DELIVERY_WEBHOOK_TIMEOUT_SECONDS must be"):
        Settings()


def test_downstream_url_accepts_exact_path_prefix(monkeypatch):
    _set_base(monkeypatch)
    monkeypatch.setenv("DOWNSTREAM_DELIVERY_ENABLED", "true")
    # URL with query param after the required prefix should be accepted
    monkeypatch.setenv(
        "N8N_INTAKE_WEBHOOK_URL",
        "http://n8n:5678/webhook/second-brain-intake?test=1",
    )
    monkeypatch.setenv("N8N_INTAKE_WEBHOOK_TOKEN", _VALID_TOKEN)
    s = Settings()
    assert s.downstream_delivery_enabled is True


# ── N8nWebhookDeliveryClient ──────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int = 202) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=MagicMock(),
                response=MagicMock(status_code=self.status_code),
            )


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass

    async def post(self, url: str, *, json: dict, headers: dict) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._response


def _make_client(response: _FakeResponse) -> tuple[N8nWebhookDeliveryClient, _FakeClient]:
    fake = _FakeClient(response)
    client = N8nWebhookDeliveryClient(
        webhook_url=_VALID_URL,
        webhook_token=_VALID_TOKEN,
        timeout_seconds=5,
    )
    return client, fake


@pytest.mark.asyncio
async def test_delivery_client_posts_capture_id_and_delivery_attempt():
    client, fake = _make_client(_FakeResponse(202))
    with patch("httpx.AsyncClient", return_value=fake):
        await client.forward_capture(capture_id="cap-001", delivery_attempt=1)

    assert len(fake.calls) == 1
    body = fake.calls[0]["json"]
    assert body["capture_id"] == "cap-001"
    assert body["delivery_attempt"] == 1


@pytest.mark.asyncio
async def test_delivery_client_sends_intake_token_header():
    client, fake = _make_client(_FakeResponse(202))
    with patch("httpx.AsyncClient", return_value=fake):
        await client.forward_capture(capture_id="cap-002", delivery_attempt=2)

    headers = fake.calls[0]["headers"]
    assert headers[INTAKE_TOKEN_HEADER] == _VALID_TOKEN


@pytest.mark.asyncio
async def test_delivery_client_posts_to_configured_url():
    client, fake = _make_client(_FakeResponse(202))
    with patch("httpx.AsyncClient", return_value=fake):
        await client.forward_capture(capture_id="cap-003", delivery_attempt=1)

    assert fake.calls[0]["url"] == _VALID_URL


@pytest.mark.asyncio
async def test_delivery_client_raises_on_5xx():
    client, fake = _make_client(_FakeResponse(500))
    with patch("httpx.AsyncClient", return_value=fake):
        with pytest.raises(httpx.HTTPStatusError):
            await client.forward_capture(capture_id="cap-004", delivery_attempt=1)


@pytest.mark.asyncio
async def test_delivery_client_raises_on_4xx():
    client, fake = _make_client(_FakeResponse(401))
    with patch("httpx.AsyncClient", return_value=fake):
        with pytest.raises(httpx.HTTPStatusError):
            await client.forward_capture(capture_id="cap-005", delivery_attempt=1)


@pytest.mark.asyncio
async def test_delivery_client_does_not_raise_on_202():
    client, fake = _make_client(_FakeResponse(202))
    with patch("httpx.AsyncClient", return_value=fake):
        # Should not raise
        await client.forward_capture(capture_id="cap-006", delivery_attempt=1)


@pytest.mark.asyncio
async def test_delivery_client_envelope_contains_no_extra_fields():
    client, fake = _make_client(_FakeResponse(202))
    with patch("httpx.AsyncClient", return_value=fake):
        await client.forward_capture(capture_id="cap-007", delivery_attempt=3)

    body = fake.calls[0]["json"]
    assert set(body.keys()) == {"capture_id", "delivery_attempt"}

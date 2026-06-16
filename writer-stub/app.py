"""
Writer-stub: standalone filing container for SB-112.

Accepts POST /write and POST /inbox from n8n, writes stub:// paths to the
capture-service ledger via acknowledge-filed / acknowledge-inbox callbacks,
then edits the Discord receipt.

A stub:// path is intentional: vault writes are not enabled in this phase.
The stub:// prefix is excluded from vault-write metrics so it does not
inflate the "last successful vault write" timestamp.
"""
from __future__ import annotations

import os
from secrets import compare_digest

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field


WRITER_TOKEN_HEADER = "X-Writer-Stub-Token"
INTERNAL_TOKEN_HEADER = "X-Second-Brain-Internal-Token"

_writer_token: str = os.environ["WRITER_STUB_INTERNAL_TOKEN"]
_capture_service_url: str = os.environ["CAPTURE_SERVICE_URL"].rstrip("/")
_capture_service_token: str = os.environ["CAPTURE_SERVICE_INTERNAL_TOKEN"]


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class WriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture_id: str = Field(min_length=1, max_length=100)
    delivery_attempt: int = Field(ge=1)


class InboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture_id: str = Field(min_length=1, max_length=100)
    delivery_attempt: int = Field(ge=1)
    reason_type: str = Field(default="", max_length=100)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "writer-stub"}


@app.post("/write", status_code=200)
async def write_note(body: WriteRequest, x_writer_stub_token: str | None = Header(default=None)):
    _require_token(x_writer_stub_token)
    stub_path = f"stub://{body.capture_id}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_capture_service_url}/internal/captures/{body.capture_id}/delivery/acknowledge-filed",
            json={"delivery_attempt": body.delivery_attempt, "note_path": stub_path},
            headers={INTERNAL_TOKEN_HEADER: _capture_service_token},
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"capture-service acknowledge-filed failed: {resp.status_code}",
        )
    return {"outcome": "stub_filed", "stub_path": stub_path}


@app.post("/inbox", status_code=200)
async def inbox_note(body: InboxRequest, x_writer_stub_token: str | None = Header(default=None)):
    _require_token(x_writer_stub_token)
    stub_path = f"stub://{body.capture_id}"
    payload: dict = {
        "delivery_attempt": body.delivery_attempt,
        "note_path": stub_path,
    }
    if body.reason_type:
        payload["reason_type"] = body.reason_type
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_capture_service_url}/internal/captures/{body.capture_id}/delivery/acknowledge-inbox",
            json=payload,
            headers={INTERNAL_TOKEN_HEADER: _capture_service_token},
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"capture-service acknowledge-inbox failed: {resp.status_code}",
        )
    return {"outcome": "stub_inbox", "stub_path": stub_path}


def _require_token(supplied: str | None) -> None:
    if supplied is None or not compare_digest(supplied, _writer_token):
        raise HTTPException(status_code=401, detail="unauthorized")

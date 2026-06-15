from __future__ import annotations

from datetime import datetime
from pathlib import Path
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from writerservice.api_models import FileNoteRequest, FileNoteResponse, HealthResponse, MoveNoteRequest, MoveNoteResponse
from writerservice.config import get_settings
from writerservice.git_errors import CaptureDuplicateError, WriterError
from writerservice.vault import check_vault_writable
from writerservice.writer import DuplicateCaptureError, VaultWriter

WRITER_TOKEN_HEADER = "X-Second-Brain-Writer-Token"


def _build_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def require_token(
        supplied: Annotated[str | None, Header(alias=WRITER_TOKEN_HEADER)] = None,
    ) -> None:
        settings = get_settings()
        if supplied is None or not compare_digest(supplied, settings.writer_service_token):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.exception_handler(WriterError)
    async def writer_error_handler(request: Request, exc: WriterError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error_type": exc.error_type,
                "retryable": exc.retryable,
                "message": str(exc),
            },
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        settings = get_settings()
        if not check_vault_writable(settings.vault_path):
            raise HTTPException(status_code=503, detail="vault path not writable")
        return HealthResponse(status="ok")

    @app.post(
        "/internal/notes/file",
        response_model=FileNoteResponse,
        dependencies=[Depends(require_token)],
    )
    async def file_note(request: FileNoteRequest) -> FileNoteResponse:
        settings = get_settings()
        vault_path = Path(settings.vault_path)

        try:
            created_at = datetime.fromisoformat(
                request.created_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid created_at format") from exc

        writer = VaultWriter(vault_path, audit_log_path=settings.audit_log_path)

        try:
            result = writer.write_note(
                capture_id=request.capture_id,
                source_message_id=request.source_message_id,
                created_at=created_at,
                classification=request.classification,
                model=request.model,
                prompt_version=request.prompt_version,
                delivery_attempt=request.delivery_attempt,
                inbox_reason=request.inbox_reason,
                git_sync_enabled=settings.git_sync_enabled,
            )
        except CaptureDuplicateError as exc:
            raise exc from exc
        except DuplicateCaptureError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error_type": "capture_id_duplicate"},
            ) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="conflicting_note") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid request") from exc

        return FileNoteResponse(
            result="FILED",
            note_path=result.note_path,
            git_commit_hash=result.git_commit_hash,
            idempotent=not result.created,
        )

    @app.post(
        "/internal/notes/move",
        response_model=MoveNoteResponse,
        dependencies=[Depends(require_token)],
    )
    async def move_note(request: MoveNoteRequest) -> MoveNoteResponse:
        settings = get_settings()
        vault_path = Path(settings.vault_path)

        writer = VaultWriter(vault_path, audit_log_path=settings.audit_log_path)

        try:
            result = writer.move_note(
                capture_id=request.capture_id,
                new_folder=request.new_folder,
                new_project=request.new_project,
                correction_reason=request.correction_reason,
                git_sync_enabled=settings.git_sync_enabled,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="note not found in vault") from exc
        except CaptureDuplicateError as exc:
            raise exc from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid request") from exc

        return MoveNoteResponse(
            result="MOVED",
            old_note_path=result.old_note_path,
            new_note_path=result.new_note_path,
            git_commit_hash=result.git_commit_hash,
        )

    return app


app = _build_app()

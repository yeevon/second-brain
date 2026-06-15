from __future__ import annotations

import logging
import re
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

logger = logging.getLogger(__name__)

# Matches both unquoted (`status: open`) and quoted (`status: "open"`) action status lines.
_OPEN_STATUS_LINE_RE = re.compile(r'^    status: "?open"?\s*$', re.MULTILINE)


def _count_open_tasks(vault_path: Path) -> int:
    count = 0
    for note_path in vault_path.rglob("*.md"):
        if not note_path.is_file():
            continue
        try:
            text = note_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        count += len(_OPEN_STATUS_LINE_RE.findall(parts[1]))
    return count

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
        logger.error(
            "WriterError %s (http=%s retryable=%s): %s",
            exc.error_type,
            exc.http_status,
            exc.retryable,
            exc,
        )
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

    @app.get(
        "/internal/vault/stats/open-tasks",
        dependencies=[Depends(require_token)],
    )
    async def vault_open_task_stats() -> dict:
        settings = get_settings()
        vault_path = Path(settings.vault_path)
        try:
            count = _count_open_tasks(vault_path)
        except Exception:
            count = None
        return {"open_tasks_count": count}

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

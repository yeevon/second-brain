from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from writerservice.api_models import ApplyProposalRequest, ApplyProposalResponse, FileNoteRequest, FileNoteResponse, HealthResponse, MoveNoteRequest, MoveNoteResponse
from writerservice.config import get_settings
from writerservice.git_errors import CaptureDuplicateError, PathTraversalError, WriterError
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


def _count_open_tasks_by_project(vault_path: Path) -> dict[str, int]:
    """Count open tasks grouped by project slug from frontmatter.

    Notes without a project field are counted under '__none__'.
    """
    by_project: dict[str, int] = {}
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
        frontmatter = parts[1]
        open_count = len(_OPEN_STATUS_LINE_RE.findall(frontmatter))
        if open_count == 0:
            continue
        project = None
        for line in frontmatter.splitlines():
            if line.startswith("project: "):
                value = line[9:].strip().strip('"')
                project = value or None
                break
        key = project if project else "__none__"
        by_project[key] = by_project.get(key, 0) + open_count
    return by_project

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
        "/internal/vault/brief/daily",
        dependencies=[Depends(require_token)],
    )
    async def vault_daily_brief() -> dict:
        from datetime import date
        from writerservice.brief import scan_daily_brief

        settings = get_settings()
        vault_path = Path(settings.vault_path)
        try:
            return scan_daily_brief(vault_path, today=date.today())
        except Exception:
            today = date.today().isoformat()
            return {
                "today": today,
                "focus_items": [],
                "due_today": [],
                "coming_up": [],
                "birthdays": [],
                "pending_tasks": [],
                "stale_tasks": [],
            }

    @app.get(
        "/internal/vault/brief/weekly",
        dependencies=[Depends(require_token)],
    )
    async def vault_weekly_brief() -> dict:
        from datetime import date, timedelta
        from writerservice.brief import scan_weekly_brief

        settings = get_settings()
        vault_path = Path(settings.vault_path)
        today = date.today()
        week_start = today - timedelta(days=7)
        try:
            return scan_weekly_brief(vault_path, week_start=week_start, week_end=today)
        except Exception:
            return {
                "week_start": week_start.isoformat(),
                "week_end": today.isoformat(),
                "accomplished": [],
                "completed_tasks": [],
                "decisions": [],
                "still_open": [],
                "study_progress": [],
            }

    @app.get(
        "/internal/vault/stats/open-tasks",
        dependencies=[Depends(require_token)],
    )
    async def vault_open_task_stats() -> dict:
        settings = get_settings()
        vault_path = Path(settings.vault_path)
        count: int | None = None
        by_project: dict[str, int] | None = None
        try:
            count = _count_open_tasks(vault_path)
            by_project = _count_open_tasks_by_project(vault_path)
        except Exception:
            pass
        return {"open_tasks_count": count, "open_tasks_by_project": by_project}

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

        from writerservice.writer import RawHashMismatchError

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
                raw_text=request.raw_text,
                attachments=list(request.attachments),
                git_sync_enabled=settings.git_sync_enabled,
            )
        except RawHashMismatchError as exc:
            logger.error("raw hash mismatch: %s", exc)
            raise HTTPException(
                status_code=409,
                detail={"error_type": "raw_hash_mismatch"},
            ) from exc
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
            raw_capture_path=result.raw_capture_path,
            raw_sha256=result.raw_sha256,
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
            result="MOVED" if result.moved else "NO_OP",
            old_note_path=result.old_note_path,
            new_note_path=result.new_note_path,
            git_commit_hash=result.git_commit_hash,
        )

    @app.post(
        "/internal/vault/apply-proposal",
        response_model=ApplyProposalResponse,
        dependencies=[Depends(require_token)],
    )
    async def apply_proposal(request: ApplyProposalRequest) -> ApplyProposalResponse:
        import httpx as _httpx
        from writerservice.flock import vault_write_lock
        from writerservice.proposal_ops import apply_proposal as _apply

        settings = get_settings()

        if not settings.capture_service_url or not settings.capture_service_internal_token:
            raise HTTPException(
                status_code=503,
                detail="capture-service not configured; cannot verify proposal state",
            )

        # Fetch proposal from capture-service and verify it is in APPLYING state
        proposal_url = (
            f"{settings.capture_service_url.rstrip('/')}"
            f"/internal/vault/proposals/{request.proposal_id}"
        )
        try:
            async with _httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(
                    proposal_url,
                    headers={"X-Second-Brain-Internal-Token": settings.capture_service_internal_token},
                )
        except _httpx.RequestError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"could not reach capture-service: {type(exc).__name__}",
            ) from exc

        if resp.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail=f"proposal {request.proposal_id} not found in capture-service",
            )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"capture-service returned {resp.status_code} for proposal lookup",
            )

        proposal = resp.json()
        if proposal.get("status") != "APPLYING":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"proposal {request.proposal_id} is not in APPLYING state "
                    f"(current status: {proposal.get('status')!r})"
                ),
            )

        vault_path = Path(settings.vault_path)
        audit_log_path = Path(settings.audit_log_path)
        lock_path = vault_path / ".vault_write.lock"

        try:
            with vault_write_lock(lock_path):
                result = _apply(
                    vault_root=vault_path,
                    proposal_id=request.proposal_id,
                    operation=proposal["operation"],
                    target_note_path=proposal["target_note_path"],
                    target_anchor_json=proposal.get("target_anchor_json"),
                    change_json=proposal["change_json"],
                    audit_log_path=audit_log_path,
                    git_sync_enabled=settings.git_sync_enabled,
                )
        except PathTraversalError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return ApplyProposalResponse(
            proposal_id=request.proposal_id,
            changed_path=result.changed_path,
            commit_hash=result.commit_hash,
            audit_record=result.audit_record,
        )

    return app


app = _build_app()

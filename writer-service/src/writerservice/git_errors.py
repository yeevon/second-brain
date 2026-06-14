from __future__ import annotations


class WriterError(Exception):
    """Base class for all writer-service failures."""
    error_type: str = "writer_error"
    http_status: int = 500
    retryable: bool = False


class GitFetchError(WriterError):
    error_type = "git_fetch_error"
    http_status = 503
    retryable = True


class GitMergeConflictError(WriterError):
    error_type = "git_merge_conflict"
    http_status = 409
    retryable = False


class GitAddError(WriterError):
    error_type = "git_add_error"
    http_status = 503
    retryable = True


class GitCommitError(WriterError):
    error_type = "git_commit_error"
    http_status = 503
    retryable = True


class GitPushRejectedError(WriterError):
    error_type = "git_push_rejected"
    http_status = 409
    retryable = True


class GitPushError(WriterError):
    error_type = "git_push_error"
    http_status = 503
    retryable = True


class GitIndexLockedError(WriterError):
    error_type = "git_index_locked"
    http_status = 503
    retryable = True


class GitWorkdirDirtyError(WriterError):
    error_type = "git_worktree_dirty"
    http_status = 503
    retryable = False


class CaptureDuplicateError(WriterError):
    error_type = "capture_id_duplicate"
    http_status = 409
    retryable = False


class PathTraversalError(WriterError):
    error_type = "path_traversal_attempt"
    http_status = 422
    retryable = False

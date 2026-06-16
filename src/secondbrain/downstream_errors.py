ALLOWED_STAGES = {
    "gemini",
    "classification_validation",
    "writer_service",
    "acknowledge_filed",
    "acknowledge_inbox",
    "capture_fetch",
    "workflow_unknown",
}

RETRYABLE_DOWNSTREAM_ERRORS = {
    "gemini_timeout",
    "gemini_rate_limit",
    "gemini_server_error",
    # writer-service Git errors that are transient and worth retrying
    "writer_git_fetch_error",
    "writer_git_add_error",
    "writer_git_commit_error",
    "writer_git_push_error",
    "writer_git_push_rejected",
    "writer_git_index_locked",
    # writer-service transport errors
    "writer_service_timeout",
    "writer_service_unavailable",
    # other retryable stages
    "capture_service_unavailable",
    "classification_validation_failure",
    "unexpected_response_shape",
    "workflow_unhandled_exception",
}

TERMINAL_DOWNSTREAM_ERRORS = {
    "defense_in_depth_secret_detected",
    "invalid_webhook_envelope",
    "contract_violation",
    # writer-service Git errors that require operator intervention
    "writer_git_conflict",
    "writer_git_worktree_dirty",
    # writer-service input errors
    "writer_path_traversal",
    "writer_capture_duplicate",
}

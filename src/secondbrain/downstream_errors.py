ALLOWED_STAGES = {
    "gemini",
    "classification_validation",
    "writer_stub",
    "acknowledge_filed",
    "acknowledge_inbox",
    "capture_fetch",
    "workflow_unknown",
}

RETRYABLE_DOWNSTREAM_ERRORS = {
    "gemini_timeout",
    "gemini_rate_limit",
    "gemini_server_error",
    "writer_stub_timeout",
    "writer_stub_unavailable",
    "capture_service_unavailable",
    "classification_validation_failure",
    "unexpected_response_shape",
    "workflow_unhandled_exception",
}

TERMINAL_DOWNSTREAM_ERRORS = {
    "defense_in_depth_secret_detected",
    "invalid_webhook_envelope",
    "contract_violation",
}

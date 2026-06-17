from types import SimpleNamespace


VALID_CLASSIFICATION = {
    "folder": "projects",
    "project": "halo",
    "note_type": "task",
    "title": "Review WebSocket reconnect handling",
    "tags": ["telemetry", "websocket"],
    "body": "Review reconnect handling in the HALO telemetry dashboard.",
    "actions": [{"text": "Review WebSocket reconnect handling", "status": "open"}],
    "needs_clarification": False,
    "clarifying_question": None,
    "confidence": 0.91,
}


class FakeClassifier:
    def __init__(self, result=None, *, error=None):
        self.aio = SimpleNamespace(models=_FakeClassifierModels(result or VALID_CLASSIFICATION, error))

    @classmethod
    def raise_error(cls, error=None):
        return cls(error=error or RuntimeError("simulated classifier failure"))

    @classmethod
    def invalid_result(cls):
        return cls({"folder": "projects", "body": "missing required fields"})

    @property
    def call_count(self):
        return self.aio.models.call_count

    @property
    def received_prompts(self):
        return self.aio.models.received_prompts

    @property
    def received_capture_ids(self):
        return ()


class _FakeClassifierModels:
    def __init__(self, result, error):
        self.result = result
        self.error = error
        self.calls = []
        self.received_prompts = []

    @property
    def call_count(self):
        return len(self.calls)

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        self.received_prompts.append(kwargs["contents"])
        if self.error is not None:
            raise self.error
        return SimpleNamespace(parsed=self.result)

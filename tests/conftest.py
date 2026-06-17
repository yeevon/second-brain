from types import SimpleNamespace

import pytest

from secondbrain.capture_service import CaptureService
from secondbrain.ledger import Ledger
from secondbrain.vault_writer import VaultWriter
from secondbrain.worker import CaptureQueue

from tests.fakes.classifier import FakeClassifier
from tests.fakes.discord import FakeDiscordChannel, FakeDiscordClient


@pytest.fixture
def test_settings(tmp_path):
    return SimpleNamespace(
        discord_bot_token="test-token",
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
        startup_reconcile_limit=10,
        classifier_queue_maxsize=10,
        gemini_api_key="fake-gemini-key",
        gemini_model="gemini-test",
        classification_confidence_threshold=0.75,
        ledger_path=tmp_path / "runtime" / "ledger.sqlite3",
        vault_path=tmp_path / "vault",
        downstream_delivery_enabled=True,
        capture_processing_mode="local-full",
        writer_service_url=None,
        writer_service_token=None,
    )


@pytest.fixture
def ledger(test_settings):
    ledger = Ledger(test_settings.ledger_path)
    try:
        yield ledger
    finally:
        ledger.close()


@pytest.fixture
def queue(test_settings):
    return CaptureQueue(maxsize=test_settings.classifier_queue_maxsize)


@pytest.fixture
def vault_writer(test_settings):
    return VaultWriter(test_settings.vault_path)


@pytest.fixture
def fake_channel():
    return FakeDiscordChannel()


@pytest.fixture
def fake_discord(fake_channel):
    return FakeDiscordClient(fake_channel)


@pytest.fixture
def fake_classifier():
    return FakeClassifier()


@pytest.fixture
def capture_service(test_settings, ledger, queue, fake_discord):
    return CaptureService(
        settings=test_settings,
        ledger=ledger,
        notify_capture=queue.enqueue,
        receipt_client=fake_discord,
    )


@pytest.fixture
def capture_handler(capture_service):
    return capture_service.handle_gateway_message

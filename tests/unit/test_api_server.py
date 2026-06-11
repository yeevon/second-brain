from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import uvicorn

from secondbrain.api_server import EmbeddedUvicornServer


def _make_server() -> EmbeddedUvicornServer:
    async def app(scope, receive, send):
        pass

    config = uvicorn.Config(app, host="127.0.0.1", port=8000)
    return EmbeddedUvicornServer(config)


@pytest.mark.asyncio
async def test_embedded_uvicorn_server_capture_signals_installs_no_handlers():
    server = _make_server()
    loop = asyncio.get_running_loop()

    with patch.object(loop, "add_signal_handler") as mock_add:
        with server.capture_signals():
            pass

    mock_add.assert_not_called()


@pytest.mark.asyncio
async def test_embedded_uvicorn_server_capture_signals_removes_no_handlers():
    server = _make_server()
    loop = asyncio.get_running_loop()

    with patch.object(loop, "remove_signal_handler") as mock_remove:
        with server.capture_signals():
            pass

    mock_remove.assert_not_called()

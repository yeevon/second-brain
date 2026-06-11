from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import uvicorn


class EmbeddedUvicornServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class InternalApiServer:
    def __init__(self, app, *, host: str, port: int) -> None:
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
        )
        self._server = EmbeddedUvicornServer(config)

    async def serve(self) -> None:
        await self._server.serve()

    async def stop(self) -> None:
        self._server.should_exit = True

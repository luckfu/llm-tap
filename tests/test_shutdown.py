import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from proxy_oneapi import main
from utils import AsyncLogger


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_stops_server_when_cancelled(self):
        class FakeServer:
            def __init__(self):
                self.stopped = False

            async def start(self):
                raise asyncio.CancelledError

            async def stop(self):
                self.stopped = True

        server = FakeServer()
        args = SimpleNamespace(
            config="config.json",
            port=12345,
            log_level="INFO",
            bind=None,
        )
        with (
            patch("proxy_oneapi.parse_args", return_value=args),
            patch("proxy_oneapi.ProxyServer", return_value=server),
            patch("proxy_oneapi._register_routes"),
        ):
            result = await main()

        self.assertIs(result, server)
        self.assertTrue(server.stopped)

    def test_async_logger_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = AsyncLogger(
                "shutdown-test",
                str(Path(directory) / "proxy.log"),
            )
            logger.close()
            logger.close()
            self.assertTrue(logger.closed)


if __name__ == "__main__":
    unittest.main()

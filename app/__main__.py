"""Entry point: `python -m app` starts the web server(s).

With TLS configured, HTTPS is served on SAI_PORT and, if SAI_HTTP_PORT is set, plain
HTTP on that port too: both run in this one process and share the same app and state.
"""
import asyncio
import contextlib
import logging
import signal

import uvicorn

from .config import settings

log = logging.getLogger("sysadmin-ia")


class _Server(uvicorn.Server):
    """uvicorn server that leaves signal handling to us, so one signal stops all listeners."""

    def install_signal_handlers(self) -> None:  # uvicorn < 0.29
        pass

    @contextlib.contextmanager
    def capture_signals(self):  # uvicorn >= 0.29
        yield


async def _serve() -> None:
    from .main import app  # import once; every listener shares the same app object

    servers: list[_Server] = []
    for port, tls in settings.listeners():
        # The local proxy (Network → Names & websites, Caddy on this machine) connects from 127.0.0.1 and says
        # in X-Forwarded-Proto/-For how the visitor really connected: trust that, and only from this machine,
        # so login cookies match the visitor's http/https and the audit log shows the visitor's address.
        kwargs = dict(host=settings.host, port=port, proxy_headers=True, forwarded_allow_ips="127.0.0.1",
                      log_level="info")
        if tls:
            kwargs.update(ssl_certfile=settings.tls_cert, ssl_keyfile=settings.tls_key)
        servers.append(_Server(uvicorn.Config(app, **kwargs)))
        log.warning("SysAdmin.IA listening on %s://%s:%d", "https" if tls else "http", settings.host, port)

    def stop() -> None:
        for s in servers:
            s.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, stop)

    await asyncio.gather(*(s.serve() for s in servers))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_serve())


if __name__ == "__main__":
    main()

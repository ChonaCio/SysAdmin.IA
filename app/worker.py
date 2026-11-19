"""One SysAdmin.IA worker process: `python -m app.worker web|jobs|network` (started by app.supervisor)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

from . import procs

log = logging.getLogger("sysadmin-ia.worker")


async def _until_stopped(*tasks: asyncio.Task) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    waiter = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()
    for t in done:
        if t is not waiter and t.exception():
            raise t.exception()


async def run_jobs() -> None:
    from . import dags
    sched = dags.SCHEDULER
    await _until_stopped(asyncio.create_task(sched.run_forever()),
                         asyncio.create_task(procs.heartbeat_loop(sched.info)))


async def run_network() -> None:
    from . import lan_names, network
    eng = network.ENGINE

    async def mdns() -> None:      # keep the .local names announced (picks up changes within seconds)
        while True:
            try:
                await asyncio.to_thread(lan_names.sync_mdns)
            except Exception:  # noqa: BLE001
                log.exception("publishing .local names failed")
            await asyncio.sleep(5)

    eng.start()
    tasks = [asyncio.create_task(mdns()), asyncio.create_task(procs.heartbeat_loop(eng.info))]
    if eng.task:
        tasks.append(eng.task)
    try:
        await _until_stopped(*tasks)
    finally:
        lan_names.stop_all()


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SAI_PROCESS", "")
    if name not in procs.WORKERS:
        sys.exit(f"usage: python -m app.worker {{{'|'.join(procs.WORKERS)}}}")
    os.environ["SAI_PROCESS"] = name
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if name == "web":
        from .__main__ import serve_web
        asyncio.run(serve_web())
    elif name == "jobs":
        asyncio.run(run_jobs())
    else:
        asyncio.run(run_network())


if __name__ == "__main__":
    main()

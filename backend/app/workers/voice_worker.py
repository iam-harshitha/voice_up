"""Voice job worker: python -m app.workers.voice_worker [--once] [--worker-id NAME]

Run any number of these; jobs are claimed with FOR UPDATE SKIP LOCKED and leased, so a
job is processed by one worker at a time and is picked up again if a worker dies.
SIGINT/SIGTERM stop the loop after the current job; unfinished jobs stay in the queue.
"""

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.errors import ServiceUnavailableError
from app.core.logging import configure_logging
from app.db.session import build_engine, build_session_factory
from app.services.voice_job_service import run_pending_jobs, schedule_reconciliations
from app.voice import build_voice_provider

logger = logging.getLogger("app.voice_worker")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process Approval Line voice jobs.")
    parser.add_argument("--once", action="store_true", help="Drain due jobs once and exit.")
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-{os.getpid()}")
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"error: invalid configuration: {exc.errors()[0]['msg']}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    try:
        provider = build_voice_provider(settings)
    except (ServiceUnavailableError, ValueError) as exc:
        logger.error("voice_worker.not_configured", extra={"reason": getattr(exc, "code", "")})
        print("error: voice provider is not configured (set VOICE_PROVIDER)", file=sys.stderr)
        return 2

    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    stop = threading.Event()

    def request_stop(signum, _frame) -> None:
        logger.info("voice_worker.stopping", extra={"signal": signum})
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    logger.info(
        "voice_worker.started", extra={"worker_id": args.worker_id, "provider": provider.name}
    )
    reconcile_every = max(30.0, settings.voice_reconcile_after_seconds / 4)
    next_reconcile = 0.0
    try:
        while not stop.is_set():
            if time.monotonic() >= next_reconcile:
                with session_factory() as session:
                    scheduled = schedule_reconciliations(session, settings)
                if scheduled:
                    logger.info("voice_worker.reconcile_scheduled", extra={"count": scheduled})
                next_reconcile = time.monotonic() + reconcile_every
            processed = run_pending_jobs(session_factory, provider, settings, args.worker_id)
            if args.once:
                break
            if not processed:
                stop.wait(settings.voice_worker_poll_seconds)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
        engine.dispose()
    logger.info("voice_worker.stopped", extra={"worker_id": args.worker_id})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

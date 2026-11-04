"""Text approval worker: python -m app.workers.text_worker [--once]

Sends queued Telegram prompts, receives button taps and /start pairing messages by long
polling (no public URL), and removes the buttons from prompts whose request has been
decided. A separate process from the voice worker: if it stops, voice is unaffected.

Run exactly one per bot: Telegram allows a single getUpdates consumer per token. Taps
are only confirmed to Telegram after they have been handled, so a restart re-reads
anything unprocessed (a replayed tap is refused by the single-use token check).
SIGINT/SIGTERM stop the loop after the current step. Never logs the bot token.
"""

import argparse
import contextlib
import logging
import signal
import sys
import threading
import time

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.errors import ServiceUnavailableError
from app.core.logging import configure_logging
from app.db.session import build_engine, build_session_factory
from app.services import text_service
from app.text import build_text_provider
from app.text.base import TextProviderError

logger = logging.getLogger("app.text_worker")

SWEEP_EVERY_SECONDS = 15.0
POLL_ERROR_BACKOFF_SECONDS = 5.0


def run_once(session_factory, provider, settings, offset: int | None, *, poll_timeout: int):
    """One iteration: send due prompts, then read and handle one batch of updates."""
    text_service.send_due_prompts(session_factory, provider, settings)
    batch = provider.get_updates(offset, poll_timeout)
    for update in batch.updates:
        try:
            result = text_service.process_update(session_factory, provider, settings, update)
        except Exception as exc:  # one bad update must never stall the queue
            logger.error("text_worker.update_failed", extra={"error": type(exc).__name__})
            continue
        logger.info(
            "text_worker.update_handled",
            extra={"accepted": result.accepted, "result": result.reason},
        )
    return batch.next_offset if batch.next_offset is not None else offset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send and receive Telegram approval prompts.")
    parser.add_argument("--once", action="store_true", help="One iteration, then exit.")
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except ValidationError as exc:
        # Validation messages name settings, never their values.
        print(f"error: invalid configuration: {exc.errors()[0]['msg']}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    if settings.text_provider == "disabled":
        print("error: TEXT_PROVIDER is disabled; nothing to do", file=sys.stderr)
        return 2
    try:
        provider = build_text_provider(settings)
    except (ServiceUnavailableError, ValueError):
        print("error: the configured text provider is invalid", file=sys.stderr)
        return 2

    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    stop = threading.Event()

    def request_stop(signum, _frame) -> None:
        logger.info("text_worker.stopping", extra={"signal": signum})
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    logger.info("text_worker.started", extra={"provider": provider.name})

    offset: int | None = None
    next_sweep = 0.0
    try:
        while not stop.is_set():
            try:
                offset = run_once(
                    session_factory,
                    provider,
                    settings,
                    offset,
                    poll_timeout=0 if args.once else settings.text_poll_timeout_seconds,
                )
            except TextProviderError as exc:
                logger.warning(
                    "text_worker.provider_error", extra={"error": exc.code, "kind": exc.kind}
                )
                if args.once:
                    return 1
                stop.wait(POLL_ERROR_BACKOFF_SECONDS)
                continue
            except Exception as exc:  # e.g. the database restarting: log the type only
                logger.error("text_worker.iteration_failed", extra={"error": type(exc).__name__})
                if args.once:
                    return 1
                stop.wait(POLL_ERROR_BACKOFF_SECONDS)
                continue
            if time.monotonic() >= next_sweep:
                text_service.close_finished_prompts(session_factory, provider)
                next_sweep = time.monotonic() + SWEEP_EVERY_SECONDS
            if args.once:
                if offset is not None:
                    # Confirm what was handled (Telegram forgets updates below the offset);
                    # anything newer stays queued for the next run.
                    with contextlib.suppress(TextProviderError):
                        provider.get_updates(offset, 0)
                break
    finally:
        provider.close()
        engine.dispose()
    logger.info("text_worker.stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

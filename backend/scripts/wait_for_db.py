"""Block until the database accepts connections, or exit non-zero after a deadline.

Usage: python scripts/wait_for_db.py [--timeout SECONDS]
Reads DATABASE_URL like the application does. Polls with a short backoff instead of a
fixed sleep, so it returns as soon as the database is ready.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    engine = create_engine(get_settings().database_url, connect_args={"connect_timeout": 3})
    deadline = time.monotonic() + args.timeout
    delay = 0.25
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
                print(f"database ready after {attempt} attempt(s)")
                return 0
            except OperationalError:
                if time.monotonic() + delay > deadline:
                    print(f"database not ready after {args.timeout:.0f}s", file=sys.stderr)
                    return 1
                time.sleep(delay)
                delay = min(delay * 2, 2.0)
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

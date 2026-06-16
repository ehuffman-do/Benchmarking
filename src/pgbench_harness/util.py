"""Small shared utilities: logging, atomic writes, run ids, redaction."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER_NAME = "pgbench-harness"


def utc_now() -> datetime:
    """Current UTC time."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (second precision)."""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def make_run_id(label: str) -> str:
    """Build a run id from the spec label plus a UTC timestamp.

    The label is slugified (lowercase alnum/dash) so the run id is always a
    safe directory name.
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-") or "run"
    return f"{slug}-{utc_now().strftime('%Y%m%dT%H%M%SZ')}"


class SecretRedactingFilter(logging.Filter):
    """Logging filter that replaces registered secrets with `***`."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []

    def register(self, secret: str) -> None:
        """Register a secret string to be redacted from all log output."""
        if secret and secret not in self._secrets:
            self._secrets.append(secret)

    def redact(self, text: str) -> str:
        """Return *text* with all registered secrets replaced by `***`."""
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(str(record.getMessage()))
        record.args = ()
        return True


_REDACTOR = SecretRedactingFilter()


def get_redactor() -> SecretRedactingFilter:
    """Process-wide secret redactor shared by logging and file writers."""
    return _REDACTOR


def setup_logging(logfile: Path | None = None, verbose: bool = False) -> logging.Logger:
    """Configure the harness logger to write to stdout and optionally a file.

    Both handlers carry the secret-redaction filter, so a secret registered
    via :func:`get_redactor` can never reach the console or the logfile.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    stream.addFilter(_REDACTOR)
    logger.addHandler(stream)
    if logfile is not None:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        fh.addFilter(_REDACTOR)
        logger.addHandler(fh)
    return logger


def get_logger() -> logging.Logger:
    """Return the harness logger (configure with :func:`setup_logging` first)."""
    return logging.getLogger(LOGGER_NAME)


def atomic_write_text(path: Path, text: str, redact: bool = True) -> None:
    """Write *text* to *path* atomically (write temp file, then rename).

    By default the registered secret is scrubbed from *text* first, so any
    file under ``results/`` is safe even if it embeds raw subprocess output
    (e.g. a libpq error that echoed connection parameters). Pass
    ``redact=False`` only for content that provably cannot contain secrets.
    """
    if redact:
        text = _REDACTOR.redact(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: Any) -> None:
    """Serialize *obj* as pretty JSON and write it atomically."""
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=False) + "\n")


def read_json(path: Path) -> Any:
    """Read and parse a JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def fmt_duration(seconds: float) -> str:
    """Format a duration in seconds as `Hh MMm SSs`."""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"

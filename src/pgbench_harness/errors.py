"""Harness error types.

All foreseeable failures raise :class:`HarnessError` subclasses; the CLI
catches these and prints a clear message plus a "what to do next" hint
instead of a traceback.
"""

from __future__ import annotations


class HarnessError(Exception):
    """Base class for all anticipated harness failures.

    Args:
        message: what happened.
        hint: what the user should do next (printed below the message).
    """

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


class SpecError(HarnessError):
    """The run spec YAML is invalid (unknown/missing keys, bad values)."""


class PreflightError(HarnessError):
    """A preflight check failed (tools, connectivity, connection ceiling, dataset)."""


class RunError(HarnessError):
    """A fatal error during run orchestration (not a single-level failure)."""


class ReportError(HarnessError):
    """Report or compare generation failed (missing/corrupt run artifacts)."""

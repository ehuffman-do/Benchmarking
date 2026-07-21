"""kubectl subprocess layer.

Every cluster interaction goes through this module as a ``kubectl`` child
process. ``KUBECONFIG`` is inherited from the process environment — the worker
injects it at exec time — and is never passed as a flag, so no command line,
log line, or error message ever carries the kubeconfig path or contents.

Design notes:
* subprocess over a Python k8s client, deliberately: command parity with the
  field-tested bash harness, and the redaction/cancel semantics of the
  existing worker apply unchanged.
* All output parsing happens here or in dedicated Python parsers — never
  shell pipelines (the bash-heredoc patronictl parsing broke in the field).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, IO, Optional

from pgbench_harness.errors import HarnessError

DEFAULT_TIMEOUT_S = 30


class KubeError(HarnessError):
    """A kubectl invocation failed (non-zero exit, timeout, or bad output)."""


@dataclass
class KubeResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def rc(self) -> int:
        """Alias for returncode. A field-crash lesson: error-formatting code
        wrote `.rc`, which only executes on the rare failure path — the
        AttributeError then masked the real failure. Support both names."""
        return self.returncode


class Kube:
    """Thin, namespace-aware kubectl runner bound to one target."""

    def __init__(self, context: str = "", namespace: str = "",
                 kubectl: str = "kubectl", timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.context = context
        self.namespace = namespace
        self.kubectl = kubectl
        self.timeout_s = timeout_s

    def _argv(self, args: list[str], namespaced: bool = True) -> list[str]:
        argv = [self.kubectl]
        if self.context:
            argv += ["--context", self.context]
        if namespaced and self.namespace:
            argv += ["-n", self.namespace]
        return argv + args

    def run(self, args: list[str], namespaced: bool = True,
            timeout_s: Optional[float] = None, check: bool = False,
            input_text: Optional[str] = None) -> KubeResult:
        """Run kubectl to completion. ``check=True`` raises KubeError on failure."""
        argv = self._argv(args, namespaced)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout_s or self.timeout_s,
                                  input=input_text)
        except subprocess.TimeoutExpired as exc:
            # keep the partial output — it is the only clue why a long exec
            # died (e.g. pgbackrest's last progress line before the stall)
            tail = ""
            for part in (exc.stderr, exc.stdout):
                if part:
                    text = part.decode("utf-8", "replace") \
                        if isinstance(part, bytes) else str(part)
                    if text.strip():
                        tail = text.strip()[-300:]
                        break
            raise KubeError(f"kubectl timed out after {timeout_s or self.timeout_s:.0f}s: "
                            f"{' '.join(args[:6])}..."
                            + (f" — last output: {tail}" if tail else ""))
        except FileNotFoundError:
            raise KubeError(f"kubectl binary not found ('{self.kubectl}') — is it installed "
                            "and on the worker's PATH?")
        res = KubeResult(argv, proc.returncode, proc.stdout, proc.stderr)
        if check and not res.ok:
            raise KubeError(f"kubectl {' '.join(args[:6])} failed (rc={res.returncode}): "
                            f"{(res.stderr or res.stdout).strip()[:500]}")
        return res

    def json(self, args: list[str], namespaced: bool = True,
             timeout_s: Optional[float] = None) -> Any:
        """Run kubectl and parse stdout as JSON (adds ``-o json`` if absent)."""
        if "-o" not in args and "--output" not in " ".join(args):
            args = args + ["-o", "json"]
        res = self.run(args, namespaced=namespaced, timeout_s=timeout_s, check=True)
        try:
            return json.loads(res.stdout)
        except ValueError as exc:
            raise KubeError(f"kubectl {' '.join(args[:4])}: output is not valid JSON: {exc}")

    def exec(self, pod: str, container: str, argv: list[str],
             timeout_s: Optional[float] = None, input_text: Optional[str] = None,
             check: bool = False) -> KubeResult:
        """``kubectl exec <pod> -c <container> -- <argv...>``."""
        args = ["exec", pod]
        if container:
            args += ["-c", container]
        if input_text is not None:
            args.insert(1, "-i")
        args += ["--"] + argv
        return self.run(args, timeout_s=timeout_s, check=check, input_text=input_text)

    def psql(self, pod: str, sql: str, database: str = "",
             timeout_s: Optional[float] = None, container: str = "database",
             csv_sep: str = "") -> KubeResult:
        """Run a query via psql inside a pod, unaligned tuples-only, quiet.

        The SQL travels as a single ``-c`` argument (never interpolated through
        a shell), and ``-X -q -A -t`` keep the output machine-parseable — the
        bash sampler's quoting bug is structurally impossible here.
        """
        argv = ["psql", "-X", "-q", "-A", "-t"]
        if csv_sep:
            argv += ["-F", csv_sep]
        if database:
            argv += ["-d", database]
        argv += ["-c", sql]
        return self.exec(pod, container, argv, timeout_s=timeout_s)

    def stream(self, args: list[str], stdout: IO[Any], namespaced: bool = True,
               stderr: Optional[IO[Any]] = None) -> subprocess.Popen:
        """Start a long-running kubectl child (logs -f, get -w, port-forward).

        The caller owns the process; because the ops runner is its own process
        group leader, a worker Stop reaps these children with one killpg.
        """
        argv = self._argv(args, namespaced)
        return subprocess.Popen(argv, stdout=stdout,
                                stderr=stderr if stderr is not None else subprocess.STDOUT,
                                text=True, bufsize=1)

    # ── convenience lookups used across ops ──

    def get_secret_value(self, name: str, key: str) -> str:
        """Decode one key of a Secret. The caller MUST register the returned
        value with the redactor before doing anything else with it."""
        import base64
        data = self.json(["get", "secret", name])
        b64 = (data.get("data") or {}).get(key)
        if not b64:
            raise KubeError(f"secret '{name}' has no key '{key}'")
        try:
            return base64.b64decode(b64).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise KubeError(f"secret '{name}' key '{key}' is not valid UTF-8: {exc}")

    def cluster_cr(self, cr_kind: str, cr_name: str) -> dict[str, Any]:
        cr = self.json(["get", cr_kind, cr_name])
        if not isinstance(cr, dict):
            raise KubeError(f"unexpected CR shape for {cr_kind}/{cr_name}")
        return cr

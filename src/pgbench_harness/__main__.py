"""Allow `python -m pgbench_harness`."""

from pgbench_harness.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

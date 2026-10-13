"""Developer tasks (invoke). Run ``uv run inv --list``."""

from __future__ import annotations

from invoke import Context, task


@task
def test(c: Context) -> None:
    """Run the unit tests."""
    c.run("uv run pytest", pty=False)


@task
def run(c: Context, args: str = "--help") -> None:
    """Run the command line tool, e.g. inv run --args='--csv readers.csv --config x.bec --dry-run'."""
    c.run(f"uv run becu {args}", pty=False)

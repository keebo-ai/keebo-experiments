"""Smoke test that keeps the project scaffold healthy."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_exists() -> None:
    assert (ROOT / "pyproject.toml").is_file()


def test_experiments_dir_exists() -> None:
    assert (ROOT / "experiments").is_dir()

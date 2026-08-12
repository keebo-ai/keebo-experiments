# Experiments

Each subdirectory here is a standalone experiment tied to a concept from the
[Keebo blog](https://keebo.ai/blog).

See the repo-root [CONTRIBUTING.md](../CONTRIBUTING.md) for the full recipe and a
copy-paste skeleton for a new experiment.

## Conventions

- One importable package per experiment: `experiments/<short_name>/` (use
  underscores so it's a valid Python package).
- Split the CLI from the logic: a thin `cli.py` (`click` commands) over a
  `benchmark.py`/`core.py` domain layer that has no `click` dependency and takes
  an open connection as an argument (so it stays testable).
- Expose a console script in the root `pyproject.toml` under `[project.scripts]`
  so the experiment runs as `poetry run <experiment-name>`.
- Every experiment includes its own `README.md` describing:
  - what it demonstrates,
  - the related blog post,
  - how to run it and what to expect.
- Shared helpers live in the root `common/` package; shared dependencies live in
  the root `pyproject.toml`.
- Never commit real credentials. Read secrets from environment variables (a
  `.env` file, loaded via `python-dotenv`; see `.env.example`).
- Mirror the source tree under `tests/unit/` so tests are easy to find.

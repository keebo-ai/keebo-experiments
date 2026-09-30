# Contributing

Thanks for helping out! This repo is a collection of small, **self-contained,
browsable** experiments that each demonstrate one concept from the
[Keebo blog](https://keebo.ai/blog). Someone should be able to clone the repo,
run one experiment end-to-end, and read its code top-to-bottom without touching
anything else.

## Ways to contribute

- **Report a bug or a surprising result.** Open an
  [issue](https://github.com/keebo-ai/keebo-experiments/issues) with the command
  you ran, what you expected, and what you saw. Mention your warehouse edition
  and cloud/region if they might matter. **Redact** account names, usernames,
  and anything else that identifies your account before you paste output.
- **Propose an experiment.** Open an issue first, before you write any code.
  Describe the claim you want to test, how you'd measure it, and roughly what
  it would cost to run. We'll agree on the scope before you build it.
- **Improve an existing experiment**, for example with sharper measurement,
  clearer output, lower cost, or better docs. Small PRs are welcome without an
  issue first.
- **Build a new experiment** by following the recipe below.

## Workflow

1. Fork the repo and create a branch from `main`.
2. `poetry install` (Python 3.14+ and Poetry 2.0+; see the root README).
3. Make your change. Keep each PR to one experiment or one fix.
4. Run the [quality gate](#quality-gate) until it's green.
5. Open a PR against `main` with a [Conventional Commits](#commits) title. In
   the description, say what changed and how you tested it. If the change
   touches warehouse compute, also say what a run costs.

CI (`.github/workflows/pr-checks.yml`) runs the same gate on every PR. A
maintainer reviews it before merge.

### Using Claude Code

This repo ships a [`CLAUDE.md`](./CLAUDE.md) and a project skill. If you use
[Claude Code](https://claude.com/claude-code), run `/add-experiment` to have it
scaffold a new experiment following this guide: the package, CLI mount, tests,
README, and quality gate. You're still the author, so review what it generates
and **run the experiment against your own account** before you open the PR.
Claude won't run anything that spends credits unless you ask it to.

## The experiment recipe

`experiments/warehouse_sizing_benchmark/` is the reference implementation.

### The shape of an experiment

Start minimal — a CLI plus one domain module:

```
experiments/<short_name>/        # underscores — it's an importable package (PEP 420, no __init__.py)
├── cli.py          # thin click command layer (the ONLY place click is imported)
├── <name>.py       # domain logic: no click, takes an open connection/client
└── README.md       # what it demonstrates, the blog post, how to run it, cost
```

Two rules do most of the work:

1. **Split the CLI from the logic.** `cli.py` parses flags, resolves
   credentials, opens a connection, and hands it to a domain function. The
   domain module has **no `click` dependency** — it raises plain `ValueError`
   and the CLI turns that into a clean `click.ClickException`.
2. **Inject the connection.** Domain functions take an already-open
   connection/client as their first argument. That's what keeps them testable
   (a fake connection) and reusable (a notebook, another experiment).

When the domain logic grows past one comfortable module, group it under a
`core/` subpackage instead — `core/queries.py`, `core/sweep.py`,
`core/report.py`, etc. The `warehouse_sizing_benchmark` experiment does this;
follow it when an experiment has that much surface, and keep the flat shape
above when it doesn't.

### Conventions (match these)

- **Cost and safety come first.** These run against *other people's* accounts.
  An experiment that uses compute works on a **dedicated** warehouse or
  resource that it creates itself, never touches existing objects, and ships a
  `cleanup` command that removes whatever it created. The command docstring
  carries a `WARNING: this uses real compute ...` line with an honest cost
  estimate, and the README has a "Before you run it" section. Prefer the
  cheapest sizes and the free `SNOWFLAKE_SAMPLE_DATA` share. Add a spend cap or
  a confirmation prompt when a run could get expensive.

- **Shared code** lives in `common/` (e.g. `common/snowflake.py`, the connection
  client). Reach for it before writing your own; extend it if the next
  experiment needs the same thing.
- **Credentials** are never passed as flags. Use the shared helpers in
  `common/credentials.py` — `connection_option` (the `--connection` flag),
  `open_connection(connection_name)`, and `resolve_credentials()` — which
  resolve, in order, from a `--connection NAME` entry in Snowflake's
  `connections.toml`, then `SNOWFLAKE_*` env vars / `.env` (via `python-dotenv`),
  then an interactive prompt for anything missing. See the warehouse
  experiment's `cli.py`. Add any new env vars to `.env.example`. Never commit
  real secrets.
- **Run output** stays out of git. Anything an experiment writes — result files,
  run manifests, exports — describes one person's warehouse rather than the
  repo, so name it `<experiment>-run-<token>.<ext>` or write it under `runs/`.
  Both patterns are already in `.gitignore`; don't add per-experiment entries.
- **Types & style:** `from __future__ import annotations`, full type hints,
  frozen dataclasses for models. Ruff (`E,F,I,UP,B`, line length 120) and
  Python 3.14.
- **Shared CLI:** mount your experiment's command on the single
  `keebo-experiments` CLI in `common/cli.py` (via `cli.add_command`), so it runs
  as `poetry run keebo-experiments <experiment> ...`. There is one console
  script for the whole repo — don't add per-experiment scripts.
- **Tests** mirror the source tree under `tests/unit/`, using click's
  `CliRunner` + `mockito`. Shared connection fakes live in `tests/conftest.py`.

### Steps

1. Create `experiments/<short_name>/` with `cli.py`, your domain module, and
   `README.md` (no `__init__.py` needed — it's a namespace package).
2. Mount your command on the shared CLI in `common/cli.py`:

   ```python
   from experiments.<short_name>.cli import <command>

   cli.add_command(<command>, "<experiment-name>")
   ```

3. Add any new dependencies to `[project.dependencies]` in the root
   `pyproject.toml`, then `poetry lock` and `poetry install`.
4. Add tests under `tests/unit/experiments/<short_name>/`, and add your command
   name to the expected set in `tests/unit/common/test_cli.py`.
5. Link the experiment from the root `README.md` "Available experiments" list.
6. Run the quality gate (below) until green, then open a PR against `main`.

### Skeleton

`experiments/<short_name>/<name>.py` (domain layer — no click):

```python
"""<one-line description>. Domain layer (no CLI dependencies)."""

from __future__ import annotations

from typing import Any


def do_the_thing(conn: Any, *, some_option: str = "default") -> list[tuple[Any, ...]]:
    """Run the experiment against an open connection and return the results."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1")  # ... the real work ...
        return list(cur.fetchall())
    finally:
        cur.close()
```

`experiments/<short_name>/cli.py` (click layer):

```python
"""<experiment> — command-line front end."""

from __future__ import annotations

import click

from common.credentials import connection_option, open_connection
from experiments.<short_name> import <name>


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@connection_option
def <command>(connection_name: str | None) -> None:
    """<what this command does, and the cost warning if it uses real compute>."""
    try:
        with open_connection(connection_name) as conn:
            for row in <name>.do_the_thing(conn):
                click.echo(row)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
```

Then mount it on the shared CLI in `common/cli.py` (see step 2). Use a
`@click.group()` instead of `@click.command()` if the experiment needs several
subcommands (like `warehouse-sizing`'s `run` / `report` / `cleanup`).

`tests/unit/experiments/<short_name>/test_<name>.py`:

```python
from __future__ import annotations

from experiments.<short_name> import <name>


def test_do_the_thing(make_cursor, make_connection):
    cursor = make_cursor(fetch=[(1,)])
    result = <name>.do_the_thing(make_connection(cursor))
    assert result == [(1,)]
    assert "SELECT 1" in cursor.executed
```

## Quality gate

Run this before every push (CI runs the same in `.github/workflows/pr-checks.yml`):

```bash
poetry install
poetry run ruff check .
poetry run ruff format --check .   # use `ruff format .` to fix
poetry run pytest
```

## Commits

[Conventional Commits](https://www.conventionalcommits.org/) — releases are
automated from them (see `.github/workflows/release.yml`). Use `feat:` for a new
experiment, `fix:` for a bug fix, `docs:`/`chore:`/`refactor:` for the rest.

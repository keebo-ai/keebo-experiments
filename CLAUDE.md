# CLAUDE.md

Guidance for Claude Code when working in this repo. It's a **public**
collection of runnable experiments for the [Keebo blog](https://keebo.ai/blog).
Anyone can clone it and run it against their own data warehouse.

[CONTRIBUTING.md](./CONTRIBUTING.md) is the source of truth for structure and
conventions. Read it before adding or restructuring an experiment. To scaffold a
new one, use the `add-experiment` skill (`/add-experiment`).

## Hard rules

- **Never spend warehouse credits on your own initiative.** Don't run a command
  that connects to a real warehouse (`run`, `report`, `cleanup`, or anything
  that calls `open_connection`) unless the user explicitly asks for that run.
  `--help` and the unit tests are always safe.
- **Never commit secrets or run output.** No `.env` files, no credentials, and
  no `*-run-*.{json,csv,txt}` files or `runs/` directories. Don't paste account
  identifiers from real output into docs, tests, or commit messages. Use made-up
  illustrative numbers instead.
- **Experiments must be safe on a stranger's account.** Use a dedicated
  warehouse or resource the experiment creates itself, include a `cleanup`
  command, and state an honest cost estimate in the command docstring
  (`WARNING: this uses real compute ...`) and in the experiment README.
- **Don't put local planning docs in the repo.** `docs/superpowers/` is
  git-ignored for that reason. Don't add design notes anywhere else.

## Architecture in one breath

`common/cli.py` is the single `keebo-experiments` click group. Each
`experiments/<short_name>/cli.py` defines one command or group and gets mounted
there with `cli.add_command(...)`. Domain logic lives in
`experiments/<short_name>/core/`. It never imports `click`, takes an open
connection as its first argument, and raises `ValueError`, which the CLI turns
into `click.ClickException`. Credentials always go through
`common/credentials.py` (`connection_option`, `open_connection`) and are never
passed as flags. Packages are PEP 420 namespace packages, so don't add
`__init__.py`.

## Tests

Tests mirror the source tree under `tests/unit/`. Use the `FakeConnection` and
`FakeCursor` fixtures in `tests/conftest.py` (`make_cursor`, `make_connection`)
plus `CliRunner` and `mockito`, and assert on the SQL that was executed. Tests
never touch a real warehouse. When you mount a new command, update
`tests/unit/common/test_cli.py`, which asserts the exact set of mounted
commands.

## Quality gate

Run this before you call anything done. CI runs the same checks:

```bash
poetry run ruff check .
poetry run ruff format --check .
poetry run pytest
```

## Commits and PRs

Use Conventional Commits, because releases are cut from them automatically:
`feat:` for a new experiment, `fix:` for a bug fix, and
`docs:`/`chore:`/`refactor:`/`test:` for everything else. Never edit
`CHANGELOG.md` or the version in `pyproject.toml` by hand. The release workflow
owns both.

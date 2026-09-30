---
name: add-experiment
description: Scaffold a new keebo-experiments experiment end-to-end (package, click CLI, core domain layer, CLI mount, tests, README, root README link) following CONTRIBUTING.md, then run the quality gate. Use when the user wants to add, create, or contribute a new experiment to this repo.
---

# Add an experiment

You're helping someone, often an outside contributor, add a new experiment to
this public repo. The result has to pass CI and read like the existing
experiments. It also has to be **safe to run on a stranger's warehouse**.

## 0. Ground yourself

Read these before you write anything:

- `CONTRIBUTING.md`, the recipe and conventions. It wins over this skill if
  they ever disagree.
- `CLAUDE.md`, which has the hard rules. The most important one: never connect
  to a real warehouse unless the user asks.
- One existing experiment end-to-end as a template. Pick the closest match:
  - `experiments/warehouse_sizing_benchmark/` is the reference implementation:
    idempotent `setup` / `cleanup` of its own guarded objects (`common/dedicated.py`),
    a parameter sweep with a hard cost cap and live stats, plus an `ACCOUNT_USAGE` report.
  - `experiments/multi_cluster_billing/` is a long-running, manifest-tracked
    experiment with async queries.
- The matching tests under `tests/unit/experiments/<name>/` and
  `tests/conftest.py`, which holds the fake connection and cursor.

## 1. Pin down the experiment

If the user hasn't already said, ask for:

1. **The claim.** What one concept does it demonstrate or test? Is there a
   related blog post or issue?
2. **The method.** What does it run, and where does the measurement come from
   (`INFORMATION_SCHEMA`, `ACCOUNT_USAGE`, or client timing)?
3. **The cost.** Which warehouse sizes, how long, and roughly how many credits?
   Does it need Enterprise edition or special grants?
4. **Names.** A package name (`snake_case`) and a CLI name (`kebab-case`), for
   example `multi_cluster_billing` / `multi-cluster-billing` or `warehouse_sizing_benchmark` /
   `warehouse-sizing`.

If there's no issue yet and the user is an outside contributor, suggest they
open one so maintainers can agree on scope. Don't block on it, though.

## 2. Scaffold

Create these files and match the chosen template's style: docstrings, comment
density, and naming.

```
experiments/<pkg>/
├── cli.py          # click group: run / report / cleanup (only file importing click)
├── core/
│   ├── queries.py  # SQL text + constants (DEFAULT_WAREHOUSE, sizes, etc.)
│   ├── run.py      # the domain logic: takes `conn` first, raises ValueError
│   └── report.py   # builds ReportTable(s) from results (common/tables.py)
└── README.md
tests/unit/experiments/<pkg>/
├── test_cli.py
├── test_queries.py
├── test_run.py
└── test_report.py
```

Stay minimal. Use the flat `cli.py` plus a single `<name>.py` shape from
CONTRIBUTING.md when the logic really is small. Don't create modules you have
nothing to put in.

Checklist for the code:

- [ ] Don't add `__init__.py` files. These are PEP 420 namespace packages.
- [ ] `from __future__ import annotations`, full type hints, and frozen
      dataclasses for models.
- [ ] `core/` never imports `click`. Every domain function takes an open
      connection first.
- [ ] `cli.py` uses `@connection_option` and `open_connection(...)` from
      `common/credentials.py`, and `echo_table` from `common/render.py`. It
      converts `ValueError` to `click.ClickException`.
- [ ] Use a **dedicated** warehouse or resource with a `DEFAULT_WAREHOUSE`
      constant and a `--warehouse` override. Never alter or drop objects the
      experiment didn't create.
- [ ] Include a `cleanup` command that drops everything the experiment created.
- [ ] The group docstring has a credentials paragraph and a
      `WARNING: this uses real compute. ...` line with the cost estimate.
- [ ] Anything written to disk is named `<experiment>-run-<token>.<ext>` or
      goes under `runs/`. Don't edit `.gitignore`.
- [ ] Add new env vars to `.env.example`. Add new dependencies to
      `[project.dependencies]` in the root `pyproject.toml`, then run
      `poetry lock`. Add as few as you can.
- [ ] Reuse `common/` before you write helpers. If another experiment will
      clearly need the same thing, extend `common/` instead of copying code.

## 3. Wire it up

1. Mount it in `common/cli.py`, keeping the imports sorted:
   ```python
   from experiments.<pkg>.cli import <command>
   cli.add_command(<command>, "<cli-name>")
   ```
2. Add `"<cli-name>"` to the expected set in `tests/unit/common/test_cli.py`.
3. Add an entry to the root `README.md` "Available experiments" list, in the
   same format as the existing entries: a bold link, one or two sentences, any
   edition requirement, and a `Run:` line.

## 4. Tests

Mirror the source tree. Use `make_cursor` / `make_connection` from
`tests/conftest.py`. Use `responses=` for sequential results and the async
helpers for `execute_async`. Assert on the SQL in `cursor.executed`. Test the
CLI with `CliRunner`, and use `monkeypatch.setattr(cli, "open_connection", ...)`
to replace the connection so no test opens a real one. See
`tests/unit/experiments/multi_cluster_billing/test_cli.py`. Cover at least:

- the happy path of each domain function, including the SQL it issues;
- `ValueError` paths and the CLI turning them into a clean error with exit
  code 1;
- `cleanup` only touching the experiment's own objects;
- report rendering with representative rows.

## 5. The experiment README

Follow `experiments/warehouse_sizing_benchmark/README.md`. Include these sections: a one-paragraph
intro, **What it demonstrates**, **⚠️ Before you run it** (honest cost ranges,
billing minimums, what gets created and dropped), **Requirements** (edition,
grants, shares), **Credentials** (link to the warehouse-sizing README's section
instead of duplicating it), **Usage** (numbered commands plus sample output marked as
illustrative), **How it's measured** or how it maps to the article, **Layout**, and **Related**. Any sample
numbers must be made up. Never paste real account output.

## 6. Quality gate

```bash
poetry run ruff format .
poetry run ruff check .
poetry run pytest
```

Iterate until all three are clean. Then confirm `poetry run keebo-experiments
<cli-name> --help` renders properly. That's safe because it doesn't connect to
anything.

## 7. Hand off

- **Don't** run `run`, `report`, or `cleanup` against a real account yourself.
  Tell the user that before they open the PR, they need to run the experiment
  on their own account, confirm the result and the cost estimate, and run
  `cleanup`.
- Suggest a Conventional Commit such as `feat: add the <cli-name> experiment`.
  If they want, help them open a PR against `main` that says what the
  experiment shows, how it was tested, and the measured cost of one run.
- Never edit `CHANGELOG.md` or the version. The release workflow owns them.

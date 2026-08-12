# Keebo Experiments

Runnable experiments that demonstrate the concepts we write about on the
[Keebo blog](https://keebo.ai/blog). They're meant to be cloned and run by
customers, prospects, and anyone curious about getting more out of their data
warehouse.

Each experiment is self-contained and reproducible so you can see the idea work
end-to-end, then adapt it to your own environment.

## Experiments

Every experiment is a subcommand of the single `keebo-experiments` CLI:

- [**warehouse-sizing**](./experiments/warehouse_sizing_benchmark/) — sweeps one
  fixed query across every Snowflake warehouse size and reads the timings and
  credits back from `ACCOUNT_USAGE`, so you can plot your own sizing curve and
  find the cost sweet spot.
  Run: `poetry run keebo-experiments warehouse-sizing --help`.
- [**multicluster-scaling**](./experiments/multicluster_scaling/) — read-only
  ($0): reads your `ACCOUNT_USAGE` history to show how Snowflake's multi-cluster
  auto-scaling actually behaved — peak clusters, how often extra ones spun up,
  how long they lived, and how busy they were.
  Run: `poetry run keebo-experiments multicluster-scaling --help`.

> **Disclaimer:** These experiments run against **your own** data warehouse and cloud accounts, and any compute, storage, or query costs they incur are **your responsibility**. Keebo makes no guarantee that any experiment will be cheap, cost-neutral, or cost-saving, and provides them "as is," without warranty of any kind. Review what an experiment does and estimate its cost before you run it.

## Requirements

- [Python](https://www.python.org/) 3.11+
- [Poetry](https://python-poetry.org/docs/#installation) 2.0+

## Getting started

```bash
# Install dependencies and the keebo-experiments console script
poetry install

# List the experiments, then run one
poetry run keebo-experiments --help
poetry run keebo-experiments multicluster-scaling --days 14

# Credentials come from a .env file (git-ignored); start from the template
cp .env.example .env
```

## Repository layout

```
keebo-experiments/
├── common/        # shared machinery used by every experiment
│   ├── cli.py         # the single `keebo-experiments` CLI (mounts each experiment)
│   ├── credentials.py # resolve creds + open a connection (env / connections.toml / prompt)
│   ├── render.py      # print report tables
│   ├── tables.py      # the ReportTable data type
│   └── snowflake.py   # Snowflake connection client (no click)
├── experiments/   # one importable package per experiment (see experiments/README.md)
│   └── <name>/
│       ├── cli.py     # click commands, registered on common/cli.py
│       └── core/      # domain logic (no click; takes a connection)
├── tests/         # unit tests, mirroring the source tree under tests/unit/
├── .env.example   # credential template
├── pyproject.toml # Poetry project, console script, and tooling config
└── README.md
```

## Adding an experiment

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the full recipe and a copy-paste
skeleton. In short:

1. Create an importable package under `experiments/`, e.g.
   `experiments/my_idea/` (underscores — it's a Python package).
2. Split a thin `cli.py` (`click`) from `click`-free domain modules under
   `core/` that take an open connection, and mount the experiment's command on
   the shared CLI in `common/cli.py`.
3. Add a `README.md` explaining what it demonstrates and linking the related
   blog post.
4. Keep shared code in `common/` and dependencies in the root `pyproject.toml`
   so everything installs with a single `poetry install`.

## Development

We use [Ruff](https://docs.astral.sh/ruff/) for linting/formatting and
[pytest](https://docs.pytest.org/) for tests:

```bash
poetry run ruff check .
poetry run ruff format .
poetry run pytest
```

## License

[MIT](./LICENSE) © Keebo

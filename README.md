# Keebo Experiments

Runnable experiments that demonstrate the concepts we write about on the
[Keebo blog](https://keebo.ai/blog). They're meant to be cloned and run by
customers, prospects, and anyone curious about getting more out of their data
warehouse.

Each experiment is self-contained and reproducible so you can see the idea work
end-to-end, then adapt it to your own environment.

## Experiments

- [**warehouse-sizing-benchmark**](./experiments/warehouse_sizing_benchmark/) —
  a `click` CLI that sweeps one fixed query across every Snowflake warehouse
  size and reads the timings and credits back from `ACCOUNT_USAGE`, so you can
  plot your own sizing curve and find the cost sweet spot.
  Run: `poetry run warehouse-sizing-benchmark --help`.
- [**multi-cluster-billing**](./experiments/multi_cluster_billing/) — a `click`
  CLI that settles whether Snowflake's 60-second billing minimum applies once
  per warehouse start or once per cluster, by driving dedicated multi-cluster
  warehouses through timed scale-out cycles and reading the bill back from
  `ACCOUNT_USAGE`. Requires the Enterprise edition.
  Run: `poetry run multi-cluster-billing --help`.

> **Disclaimer:** These experiments run against **your own** data warehouse and cloud accounts, and any compute, storage, or query costs they incur are **your responsibility**. Keebo makes no guarantee that any experiment will be cheap, cost-neutral, or cost-saving, and provides them "as is," without warranty of any kind. Review what an experiment does and estimate its cost before you run it.

## Requirements

- [Python](https://www.python.org/) 3.11+
- [Poetry](https://python-poetry.org/docs/#installation) 2.0+

## Getting started

```bash
# Install dependencies and the experiment console scripts
poetry install

# List what's available, then run an experiment
poetry run warehouse-sizing-benchmark --help

# Credentials come from a .env file (git-ignored); start from the template
cp .env.example .env
```

## Repository layout

```
keebo-experiments/
├── common/        # shared helpers (e.g. the Snowflake connection client)
├── experiments/   # one importable package per experiment (see experiments/README.md)
│   └── warehouse_sizing_benchmark/
│       ├── cli.py        # click command layer
│       └── benchmark.py  # domain logic (no click; takes a connection)
├── tests/         # unit tests, mirroring the source tree under tests/unit/
├── .env.example   # credential template
├── pyproject.toml # Poetry project, console scripts, and tooling config
└── README.md
```

## Adding an experiment

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the full recipe and a copy-paste
skeleton. In short:

1. Create an importable package under `experiments/`, e.g.
   `experiments/my_idea/` (underscores — it's a Python package).
2. Split a thin `cli.py` (`click`) from a `click`-free domain module that takes
   an open connection, and register a console script in `[project.scripts]`.
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

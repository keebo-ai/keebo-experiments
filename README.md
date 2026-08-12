# Keebo Experiments

Runnable experiments that demonstrate the concepts we write about on the
[Keebo blog](https://keebo.ai/blog). They're meant to be cloned and run by
customers, prospects, and anyone curious about getting more out of their data
warehouse.

Each experiment measures how your warehouse *really* behaves — with honest,
reproducible methods — so you can see the idea work end-to-end, then adapt it to
your own environment.

> **Disclaimer:** These experiments run against **your own** data warehouse and cloud accounts, and any compute, storage, or query costs they incur are **your responsibility**. Keebo makes no guarantee that any experiment will be cheap, cost-neutral, or cost-saving, and provides them "as is," without warranty of any kind. Review what an experiment does and estimate its cost before you run it.

## Requirements

- [Python](https://www.python.org/) 3.14+
- [Poetry](https://python-poetry.org/docs/#installation) 2.0+

## Getting started

```bash
# Install into a virtual environment
poetry install

# Configure your warehouse connection (git-ignored)
cp .env.example .env && edit .env

# List the experiments, then run one
poetry run keebo-experiments --help
poetry run keebo-experiments warehouse-sizing --help
```

Every experiment is a subcommand of the single `keebo-experiments` CLI.

## Available experiments

- [**warehouse-sizing**](./experiments/warehouse_sizing_benchmark/) — sweeps one
  fixed query across every Snowflake warehouse size and reads the timings and
  credits back from `ACCOUNT_USAGE`, so you can plot your own sizing curve and
  find the cost sweet spot.
  Run: `poetry run keebo-experiments warehouse-sizing --help`.
- [**multicluster-demo**](./experiments/multicluster_demo/) — **spends credits**:
  runs the same batch of concurrent queries with one cluster vs many to show
  multi-cluster scale-out cutting queue time and wall-clock. `run --estimate`
  prints the cost first; the temporary warehouse is dropped automatically.
  Run: `poetry run keebo-experiments multicluster-demo --help`.

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

See [`experiments/README.md`](./experiments/README.md) and
[CONTRIBUTING.md](./CONTRIBUTING.md). Keep dependencies in the root
`pyproject.toml` so everything installs with a single `poetry install`.

## Development

We use [Ruff](https://docs.astral.sh/ruff/) for linting/formatting and
[pytest](https://docs.pytest.org/) for tests:

```bash
poetry run ruff format .
poetry run ruff check .
poetry run pytest
```

## License

[MIT](./LICENSE) © Keebo

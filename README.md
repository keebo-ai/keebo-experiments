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
  fixed query across Snowflake warehouse sizes, shows each size's runtime,
  disk spill, and credits live, and reads the billed credits back from
  `ACCOUNT_USAGE`, so you can plot your own sizing curve and find the cost
  sweet spot. Pick two sizes for a side-by-side of what spill costs (the
  spillage webinar demo). It creates and drops its own warehouse and database,
  and caps each run's credits.
  Run: `poetry run keebo-experiments warehouse-sizing --help`.
- [**multi-cluster-billing**](./experiments/multi_cluster_billing/) — settles
  whether Snowflake's 60-second billing minimum applies once per warehouse start
  or once per cluster, by driving dedicated multi-cluster warehouses through
  timed scale-out cycles and reading the bill back from `ACCOUNT_USAGE`. Requires
  the Enterprise edition.
  Run: `poetry run keebo-experiments multi-cluster-billing --help`.

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

## Contributing

Contributions from the community are welcome, whether that's a bug report, an
idea for an experiment, a fix, or a whole new experiment.

- **Found a bug or an odd result?** [Open an issue](https://github.com/keebo-ai/keebo-experiments/issues)
  with the command you ran and what you saw. Redact your account details first.
- **Have an experiment idea?** Open an issue that describes the claim you want
  to test and roughly what it would cost to run, so we can agree on scope before
  you build it.
- **Ready to code?** Fork the repo, branch from `main`, follow the recipe in
  [CONTRIBUTING.md](./CONTRIBUTING.md), make sure the
  [quality gate](#development) passes, and open a PR with a
  [Conventional Commits](https://www.conventionalcommits.org/) title.

**Using [Claude Code](https://claude.com/claude-code)?** The repo includes a
[`CLAUDE.md`](./CLAUDE.md) with the project rules and an `/add-experiment` skill
that scaffolds a new experiment the way CONTRIBUTING.md describes. Review what
it generates and test it against your own account before you open the PR.

Every experiment must be safe to run on someone else's account. It works on a
dedicated warehouse, ships a `cleanup` command, and states its cost up front.
See [CONTRIBUTING.md](./CONTRIBUTING.md#conventions-match-these) for the rest.

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

# Keebo Experiments

Runnable experiments that demonstrate the concepts we write about on the
[Keebo blog](https://keebo.ai/blog). They're meant to be cloned and run by
customers, prospects, and anyone curious about getting more out of their data
warehouse.

Each experiment is self-contained and reproducible so you can see the idea work
end-to-end, then adapt it to your own environment.

## Requirements

- [Python](https://www.python.org/) 3.11+
- [Poetry](https://python-poetry.org/docs/#installation) 2.0+

## Getting started

```bash
# Install dependencies into a virtual environment
poetry install

# Run a command inside the environment
poetry run python -m pytest
```

## Repository layout

```
keebo-experiments/
├── experiments/   # one directory per experiment (see experiments/README.md)
├── tests/         # smoke tests that keep the scaffold healthy
├── pyproject.toml # Poetry project + tooling config
└── README.md
```

## Adding an experiment

1. Create a new directory under `experiments/`, e.g. `experiments/my-idea/`.
2. Add a `README.md` explaining what it demonstrates and linking the related
   blog post.
3. Keep dependencies in the root `pyproject.toml` so everything installs with a
   single `poetry install`.

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

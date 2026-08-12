# Experiments

Each subdirectory here is a standalone experiment tied to a concept from the
[Keebo blog](https://keebo.ai/blog).

## Conventions

- One directory per experiment: `experiments/<short-name>/`.
- Every experiment includes its own `README.md` describing:
  - what it demonstrates,
  - the related blog post,
  - how to run it and what to expect.
- Shared dependencies live in the root `pyproject.toml`.
- Never commit real credentials. Read secrets from environment variables.

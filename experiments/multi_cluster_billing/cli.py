"""Multi-cluster billing test — command-line front end.

Thin ``click`` wrappers over the domain layer in :mod:`core`. Each command
resolves Snowflake credentials from the environment, opens a connection, and
hands it to a domain function. Credentials are read from ``.env`` (or the real
environment); no secrets are passed as flags.

Installed as the ``multi-cluster-billing`` console script (see
``pyproject.toml``), so it runs as::

    poetry run multi-cluster-billing run
    poetry run multi-cluster-billing report
    poetry run multi-cluster-billing cleanup
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click
from dotenv import load_dotenv

from common import snowflake as sf
from experiments.multi_cluster_billing.core import manifest, queries, questions, scenarios, verdict
from experiments.multi_cluster_billing.core import report as report_core

# Load .env once, so credentials can live in a file that git ignores.
load_dotenv()

_CONNECTION_OPTION = click.option(
    "--connection",
    "connection_name",
    default=None,
    help=(
        "Name of an entry in Snowflake's connections.toml to connect with. "
        "If omitted, credentials come from SNOWFLAKE_* env / .env, prompting "
        "for anything missing."
    ),
)
_MANIFEST_OPTION = click.option(
    "--manifest",
    "manifest_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Run manifest to use. Defaults to the most recent one in this directory.",
)


def _resolve_credentials() -> sf.SnowflakeCredentials:
    """Build credentials from the environment, prompting for what's missing."""
    env = sf.env_credentials()
    account = env["account"] or click.prompt("Snowflake account")
    user = env["user"] or click.prompt("Snowflake user")
    password = env["password"]
    authenticator = env["authenticator"]
    # Need one credential; prompt for a password only if SSO isn't configured.
    if not password and not authenticator:
        password = click.prompt("Snowflake password", hide_input=True)
    return sf.SnowflakeCredentials(
        account=account,
        user=user,
        password=password,
        role=env["role"],
        authenticator=authenticator,
    )


@contextmanager
def _open(connection_name: str | None) -> Iterator[Any]:
    """Open a connection, as a context manager."""
    if connection_name:
        with sf.connection(connection_name=connection_name) as conn:
            yield conn
    else:
        with sf.connection(creds=_resolve_credentials()) as conn:
            yield conn


def _resolve_manifest(manifest_path: str | None) -> Path:
    if manifest_path:
        return Path(manifest_path)
    found = manifest.latest_path(".")
    if found is None:
        raise click.ClickException("No run manifest found. Run `multi-cluster-billing run` first, or pass --manifest.")
    return found


#: Width the prose is wrapped to. Narrow enough to stay readable in a terminal
#: and in the report file, which is read in both.
_PROSE_WIDTH = 96


def _table_lines(table: report_core.ReportTable) -> list[str]:
    """One report step as an aligned table."""
    lines = ["", f"--- Step {table.step}. {table.title} ---"]
    if not table.rows:
        return [*lines, "  (no rows yet — ACCOUNT_USAGE may still be catching up)"]

    cells_text = [[("" if value is None else str(value)) for value in row] for row in table.rows]
    widths = [max(len(table.columns[i]), *(len(row[i]) for row in cells_text)) for i in range(len(table.columns))]
    lines.append("  " + "  ".join(name.ljust(widths[i]) for i, name in enumerate(table.columns)))
    lines.append("  " + "  ".join("-" * width for width in widths))
    lines.extend("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(table.columns))) for row in cells_text)
    return lines


def _prose_lines(paragraphs: list[str]) -> list[str]:
    """Wrap the plain-English verdict, one blank line between paragraphs."""
    lines: list[str] = []
    for paragraph in paragraphs:
        lines.append("")
        lines.extend(textwrap.wrap(paragraph, width=_PROSE_WIDTH, initial_indent="  ", subsequent_indent="  "))
    return lines


#: Width of the label column in the question and scenario blocks. Wide enough for
#: the longest label, so the text after every label starts in the same place.
_LABEL_WIDTH = 16


def _labelled(label: str, text: str, *, indent: str = "      ") -> list[str]:
    """One ``Label: text`` entry, wrapped, with the text kept in its own column."""
    head = indent + label.ljust(_LABEL_WIDTH)
    wrapped = textwrap.wrap(text, width=_PROSE_WIDTH, initial_indent=head, subsequent_indent=" " * len(head))
    return wrapped or [head.rstrip()]


def _meter_lines(check: verdict.MeterCheck) -> list[str]:
    """What the bills were decoded with, and the evidence that it was right."""
    verdict_line = (
        f"  all {check.n} bill(s) decode to whole seconds (worst gap {check.worst_gap_seconds:.3f}s), which is "
        "what a correct rate looks like"
        if check.ok
        else f"  {check.worst_warehouse} is {check.worst_gap_seconds:.3f}s away from a whole second — the rate is wrong"
    )
    return [
        "",
        "--- credit rate ---",
        f"  bills decoded at the published rate for {check.size} on {check.resource_constraint}: "
        f"{check.published:.4f} credits/hour",
        verdict_line,
    ]


def _scenario_numbers(result: verdict.ScenarioResult) -> str:
    """The measurement itself, in one sentence, before anything is concluded from it."""
    parts = [f"the warehouse ran {result.warehouse_seconds:.1f}s"]
    if result.extra_clusters == 0:
        parts.append("no cluster beyond the first")
    elif result.extra_clusters == 1:
        parts.append(f"one extra cluster ran {result.extra_seconds:.1f}s")
    else:
        parts.append(f"{result.extra_clusters} extra clusters ran {result.extra_seconds:.1f}s between them")
    parts.append(f"the bill was {result.billed_seconds:.1f}s")
    if result.extra_clusters:
        parts.append(f"of which {result.extra_charge:.1f}s is what the extra clusters added")
    return ", ".join(parts) + f" (averaged over {result.n} replicate(s))"


def _scenario_blocks(result: verdict.Verdict) -> list[str]:
    """Every scenario in its own words: what it ran, why, what it measured, what that settles.

    Each block stands on its own, so the argument can be followed one measurement
    at a time instead of only as a finished verdict.
    """
    lines = ["", "--- what each scenario ran, and what its numbers say ---"]
    for scenario in result.scenarios:
        spec = queries.SCENARIOS_BY_NAME.get(scenario.name)
        lines.append("")
        lines.append(f"  {scenario.name}")
        if spec is not None:
            lines.extend(_labelled("What it ran:", spec.does))
            lines.extend(_labelled("Why:", spec.why))
        lines.extend(_labelled("Numbers:", _scenario_numbers(scenario)))
        lines.extend(_labelled("Conclusion:", verdict.scenario_conclusion(scenario)))
    return lines


def _question_evidence(question: questions.Question, by_name: dict[str, verdict.ScenarioResult]) -> str:
    """What the scenarios behind one question actually measured."""
    parts = []
    for name in question.scenarios:
        scenario = by_name.get(name)
        if scenario is None or not scenario.n:
            parts.append(f"`{name}` produced no usable replicate")
            continue
        parts.append(
            f"in `{name}`, {scenario.extra_seconds:.1f}s of extra cluster added "
            f"{scenario.extra_charge:.1f}s to the bill"
        )
    return "; ".join(parts) + "."


def _question_blocks(result: verdict.Verdict) -> list[str]:
    """The questions the run was built to answer, each with its evidence and its answer."""
    by_name = {scenario.name: scenario for scenario in result.scenarios}
    lines = ["", "--- the questions this run set out to answer ---"]
    for number, (question, answer) in enumerate(result.answers, start=1):
        lines.append("")
        lines.extend(
            textwrap.wrap(
                f"Q{number}. {question.text}",
                width=_PROSE_WIDTH,
                initial_indent="  ",
                subsequent_indent="      ",
            )
        )
        lines.extend(_labelled("Why it matters:", question.why))
        lines.extend(_labelled("What we saw:", _question_evidence(question, by_name)))
        lines.extend(_labelled("Answer:", answer))
    return lines


def _report_lines(summary: report_core.ReportSummary) -> list[str]:
    """The whole report as text: tables, checks, the answer in words, the numbers.

    Built as lines rather than echoed as it goes, so the terminal and the report
    file get the same thing without the report being rendered twice.
    """
    lines: list[str] = []
    for table in summary.tables:
        lines.extend(_table_lines(table))

    if summary.meter_check is not None:
        lines.extend(_meter_lines(summary.meter_check))

    if summary.minimum_check is not None and not summary.minimum_check.holds:
        lines.extend(
            [
                "",
                f"WARNING: the `short` scenario billed {summary.minimum_check.mean_billed:.1f}s for "
                f"{summary.minimum_check.mean_warehouse_seconds:.1f}s of warehouse. There is no 60-second "
                "minimum here, so the premise of the experiment does not hold and nothing below means what "
                "it says.",
            ]
        )

    if summary.not_ready_reason:
        return [*lines, "", f"No verdict yet: {summary.not_ready_reason}"]

    result = summary.verdict
    lines.extend(["", f"=== Verdict: {result.outcome} ==="])
    lines.extend(_prose_lines(verdict.explain(result, minimum=summary.minimum_check)))
    lines.extend(_question_blocks(result))
    lines.extend(_scenario_blocks(result))
    return lines


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Find out what a Snowflake warehouse's extra clusters actually cost.

    \b
    The questions it answers:
      - Does suspending an extra cluster before it has run a minute save anything?
      - Does it matter when an extra cluster starts?
      - What does an extra cluster that runs longer than a minute cost?
      - Does every extra cluster carry a minute of its own, or do they share one?

    \b
    Typical flow:
        multi-cluster-billing run      # drive the warehouses, write a manifest
        multi-cluster-billing report   # read the bill back (retry until it lands)
        multi-cluster-billing cleanup  # drop the test warehouses

    Credentials: pass --connection NAME to use an entry from Snowflake's
    connections.toml, or set SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER /
    SNOWFLAKE_PASSWORD (or SNOWFLAKE_AUTHENTICATOR) / SNOWFLAKE_ROLE in the
    environment or a .env file (see .env.example). Anything missing is prompted
    for. SNOWFLAKE_ROLE needs ACCOUNT_USAGE access for the report.

    WARNING: this uses real compute. A default run takes about an hour and bills
    roughly 2 credits. Multi-cluster warehouses require the Enterprise edition or
    higher.
    """


@cli.command()
@click.option(
    "--replicates",
    default=queries.DEFAULT_REPLICATES,
    show_default=True,
    type=click.IntRange(min=1),
    help="Replicates per scenario. Four is enough to show a bill is repeatable rather than a one-off.",
)
@click.option(
    "--resource-constraint",
    default=queries.DEFAULT_RESOURCE_CONSTRAINT,
    show_default=True,
    type=click.Choice(sorted(queries.RESOURCE_CONSTRAINTS), case_sensitive=False),
    help="Warehouse generation to pin. Gen2 bills 1.35x Gen1; inheriting it is what made v1 inconclusive.",
)
@click.option("--no-natural", is_flag=True, help="Skip the natural scale-out cross-check.")
@click.option(
    "--natural-rowcount",
    default=queries.DEFAULT_NATURAL_ROWCOUNT,
    show_default=True,
    type=click.IntRange(min=1),
    help="Rows generated by each natural-scenario query. Raise it if the queries finish too fast to queue.",
)
@_MANIFEST_OPTION
@_CONNECTION_OPTION
@click.option("--yes", is_flag=True, help="Skip the cost confirmation.")
def run(
    replicates: int,
    resource_constraint: str,
    no_natural: bool,
    natural_rowcount: int,
    manifest_path: str | None,
    connection_name: str | None,
    yes: bool,
) -> None:
    """Drive the warehouses and write a run manifest."""
    if not yes:
        click.confirm(
            "This creates real warehouses, takes about an hour, and bills roughly 2 credits. Continue?",
            abort=True,
        )

    token_path: Path | None = Path(manifest_path) if manifest_path else None

    def checkpoint(record: manifest.RunManifest) -> None:
        # Written after every warehouse: 29 warehouses over about an hour is a
        # long window to crash in, and `cleanup` can only drop what it can read.
        nonlocal token_path
        if token_path is None:
            token_path = manifest.default_path(record.run_token)
        manifest.save(record, token_path)

    try:
        with _open(connection_name) as conn:
            run_record = scenarios.run_experiment(
                conn,
                replicates=replicates,
                resource_constraint=resource_constraint,
                include_natural=not no_natural,
                natural_rowcount=natural_rowcount,
                echo=click.echo,
                checkpoint=checkpoint,
            )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    ready_by = manifest.metering_ready_by(run_record)
    click.echo(f"\nManifest written to {token_path}")
    click.echo(f"Metering lands by {ready_by.isoformat()} at the latest, and often much sooner.")
    click.echo("Run this whenever you like; it says what is still missing:  multi-cluster-billing report")
    click.echo("When you're done:  multi-cluster-billing cleanup")


@cli.command()
@_MANIFEST_OPTION
@_CONNECTION_OPTION
@click.option(
    "--out",
    "out_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Where to write the report. Defaults to the manifest's name with a .txt extension.",
)
@click.option("--no-file", is_flag=True, help="Print the report without writing it to a file.")
def report(manifest_path: str | None, connection_name: str | None, out_path: str | None, no_file: bool) -> None:
    """Read the bill back from ACCOUNT_USAGE, print the verdict, and write it out."""
    path = _resolve_manifest(manifest_path)
    try:
        run_record = manifest.load(path)
        with _open(connection_name) as conn:
            summary = report_core.read_report(conn, run_record)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    lines = _report_lines(summary)
    for line in lines:
        click.echo(line)

    if no_file:
        return
    # Written whether or not there is a verdict yet: a report that says what is
    # still missing is the thing you want to keep between retries.
    target = Path(out_path) if out_path else manifest.report_path(run_record.run_token, path.parent)
    header = [
        f"Multi-cluster billing test — run {run_record.run_token}",
        f"account {run_record.account} ({run_record.region}), Snowflake {run_record.snowflake_version}",
        f"{run_record.size} {run_record.resource_constraint}, started {run_record.started_at}",
        f"manifest {path}",
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join([*header, *lines]) + "\n")
    click.echo(f"\nReport written to {target}")


@cli.command()
@_MANIFEST_OPTION
@_CONNECTION_OPTION
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
def cleanup(manifest_path: str | None, connection_name: str | None, yes: bool) -> None:
    """Drop the warehouses this run created, and nothing else."""
    path = _resolve_manifest(manifest_path)
    try:
        run_record = manifest.load(path)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if not yes:
        click.confirm(f"Drop {len(run_record.warehouses)} warehouse(s) from {path}?", abort=True)
    try:
        with _open(connection_name) as conn:
            scenarios.drop_warehouses(conn, warehouses=run_record.warehouses, echo=click.echo)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    cli()

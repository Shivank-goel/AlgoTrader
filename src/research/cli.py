"""Offline experiment registration, legacy import and crash recovery."""

import json
from pathlib import Path

import click

from src.research.models import ExperimentSpec
from src.research.registry import ExperimentRegistry, verify_artifacts


@click.group()
@click.option("--database", type=click.Path(path_type=Path), default="data/research/experiments.sqlite3")
@click.pass_context
def research(ctx, database: Path):
    """Offline research evidence; never enables broker access."""
    ctx.ensure_object(dict)
    ctx.obj["research_database"] = database


@research.command()
@click.argument("name")
@click.option("--budget", type=click.IntRange(min=1), required=True)
@click.pass_context
def campaign(ctx, name: str, budget: int):
    """Freeze an experiment budget before testing."""
    registry = ExperimentRegistry(ctx.obj["research_database"])
    try:
        registry.campaign(name, budget)
    finally:
        registry.close()
    click.echo("Campaign registered")


@research.command()
@click.argument("campaign_id")
@click.argument("specification", type=click.Path(exists=True, path_type=Path))
@click.option("--root", type=click.Path(exists=True, path_type=Path), default=".")
@click.pass_context
def register(ctx, campaign_id: str, specification: Path, root: Path):
    """Freeze a specification after verifying its artifact manifest."""
    spec = ExperimentSpec.model_validate_json(specification.read_text())
    verify_artifacts(root, spec)
    registry = ExperimentRegistry(ctx.obj["research_database"])
    try:
        registry.register(campaign_id, spec)
    finally:
        registry.close()
    click.echo(spec.experiment_id)


@research.command("import-legacy")
@click.argument("trials", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def import_legacy(ctx, trials: Path):
    """Preserve a validated historical snapshot without duplicating trial counts."""
    registry = ExperimentRegistry(ctx.obj["research_database"])
    try:
        click.echo(f"Preserved {registry.import_legacy(trials)} historical trial rows")
    finally:
        registry.close()


@research.command("report")
@click.argument("experiment_id")
@click.option("--trials", type=click.Path(path_type=Path), default="data/trials.json")
@click.pass_context
def show_report(ctx, experiment_id: str, trials: Path):
    registry = ExperimentRegistry(ctx.obj["research_database"])
    try:
        click.echo(json.dumps(registry.report_view(experiment_id, trials), indent=2))
    finally:
        registry.close()


@research.command("status")
@click.option("--campaign", default=None)
@click.pass_context
def status(ctx, campaign: str | None):
    """List unfinished, unpublished and inconsistent experiment evidence."""
    registry = ExperimentRegistry(ctx.obj["research_database"])
    try:
        click.echo(json.dumps({"schema_version": 3, "experiments": registry.status(campaign),
                               "holdout_access": registry.holdout_access(campaign)}, indent=2))
    finally:
        registry.close()


@research.command("resolve-interruption")
@click.argument("experiment_id")
@click.option("--trials", type=click.Path(path_type=Path), default="data/trials.json")
@click.option("--operator", required=True)
@click.option("--reason", required=True)
@click.pass_context
def resolve_interruption(ctx, experiment_id: str, trials: Path, operator: str, reason: str):
    """Record an abandoned attempt as failed and count it, without rerunning it."""
    from src.research.runner import resolve_interruption as resolve
    registry = ExperimentRegistry(ctx.obj["research_database"])
    try:
        click.echo(json.dumps(resolve(registry, experiment_id, trials, operator=operator, reason=reason), indent=2))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        registry.close()


@research.command("recover-publication")
@click.argument("experiment_id")
@click.option("--trials", type=click.Path(exists=True, path_type=Path), default="data/trials.json")
@click.pass_context
def recover_publication(ctx, experiment_id: str, trials: Path):
    """Publish an already durable result; never repeat an interrupted backtest."""
    from src.research.runner import publish_trial
    registry = ExperimentRegistry(ctx.obj["research_database"])
    try:
        click.echo(json.dumps(publish_trial(registry, experiment_id, trials), indent=2))
    finally:
        registry.close()


@research.command("run")
@click.argument("experiment_id")
@click.option("--adapter", type=click.Choice(["csv_xs_momentum", "csv_nse_regime", "bhavcopy_nse_regime"]), required=True)
@click.option("--root", type=click.Path(exists=True, path_type=Path), default=".")
@click.option("--trials", type=click.Path(path_type=Path), default="data/trials.json")
@click.pass_context
def run_experiment(ctx, experiment_id: str, adapter: str, root: Path, trials: Path):
    """Run one registered experiment through an allowlisted offline adapter."""
    from src.research.adapters import run_adapter
    from src.research.runner import run_once
    registry = ExperimentRegistry(ctx.obj["research_database"])
    try:
        result = run_once(registry, experiment_id, root, trials,
                          lambda spec: run_adapter(adapter, spec, root))
        click.echo(json.dumps(result, indent=2))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        registry.close()


@research.command("evaluate-holdout")
@click.argument("experiment_id")
@click.option("--adapter", type=click.Choice(["csv_xs_momentum", "csv_nse_regime", "bhavcopy_nse_regime"]), required=True)
@click.option("--root", type=click.Path(exists=True, path_type=Path), default=".")
@click.option("--trials", type=click.Path(path_type=Path), default="data/trials.json")
@click.pass_context
def evaluate_holdout(ctx, experiment_id: str, adapter: str, root: Path, trials: Path):
    """Consume one pre-registered sealed holdout and record the irreversible access."""
    from src.research.adapters import run_adapter
    from src.research.runner import run_once
    registry = ExperimentRegistry(ctx.obj["research_database"])
    try:
        access = registry.authorize_holdout(experiment_id)
        result = run_once(registry, experiment_id, root, trials,
                          lambda spec: run_adapter(adapter, spec, root), holdout=True)
        click.echo(json.dumps({"holdout_access": access, "report": result}, indent=2))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        registry.close()

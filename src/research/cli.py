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


@research.command("nse-preflight")
@click.option("--root", type=click.Path(exists=True, path_type=Path), default=".")
def nse_preflight(root: Path):
    """Audit canonical NSE trial prerequisites without registering or running them."""
    from src.research.nse_trials import audit_preregistration
    click.echo(json.dumps(audit_preregistration(root.resolve()), indent=2))


@research.group("nse-trial")
def nse_trial() -> None:
    """Inspect proposed NSE trials; execution is intentionally separate."""


@nse_trial.command("status")
@click.option("--root", type=click.Path(exists=True, path_type=Path), default=".")
def nse_trial_status(root: Path) -> None:
    from src.research.nse_trials import audit_preregistration
    click.echo(json.dumps({"proposals": audit_preregistration(root.resolve()),
                           "executed": 0, "registered": 0}, indent=2))


@nse_trial.command("register")
@click.argument("specification", type=click.Path(exists=True, path_type=Path))
@click.option("--dataset-hash", required=True, help="Hash read from the verified DATA_READY manifest.")
@click.option("--database", type=click.Path(path_type=Path), default="data/research/experiments.sqlite3")
def nse_trial_register(specification: Path, dataset_hash: str, database: Path) -> None:
    """Register one proposed primary trial; never execute it."""
    from src.research.nse_trials import NseTrialRegistry
    try:
        spec = json.loads(specification.read_text())
        result = NseTrialRegistry(database).register(spec, dataset_hash=dataset_hash)
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(json.dumps(result, indent=2))


@nse_trial.command("run")
@click.argument("trial_id")
@click.option("--database", type=click.Path(path_type=Path), default="data/research/experiments.sqlite3")
@click.option("--dataset-dir", type=click.Path(path_type=Path), default="data/fyers/daily-bars")
def nse_trial_run(trial_id: str, database: Path, dataset_dir: Path) -> None:
    """Execute a registered trial offline; fail closed on missing semantics."""
    from src.research.nse_trials import NseTrialRegistry, execute_registered_trial
    try:
        NseTrialRegistry(database).get(trial_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    try:
        click.echo(json.dumps(execute_registered_trial(NseTrialRegistry(database), trial_id,
                                                       dataset_dir=dataset_dir), indent=2))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None


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

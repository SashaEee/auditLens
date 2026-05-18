from __future__ import annotations
import json, os, sys
import click
from .logging_setup import setup
from .orchestrator import runner
from .quality.checks import run_quality

log = setup()

@click.group()
def cli(): ...

@cli.command()
@click.option("--source", required=True, help="ключ из config/sources.yaml")
@click.option("--target", default=None, help="имя конкретного target (опционально)")
@click.option("--openclaw-job", default=None, envvar="OPENCLAW_JOB",
              help="ID job-а из openclaw/jobs/*.yaml")
def ingest(source: str, target: str | None, openclaw_job: str | None):
    """Запустить ingest источника."""
    res = runner.ingest(source, target, openclaw_job)
    click.echo(json.dumps(res, ensure_ascii=False))

@cli.command()
def quality():
    """Прогнать data-quality чеки и записать отчёт."""
    res = run_quality()
    click.echo(json.dumps(res, ensure_ascii=False, default=str))

@cli.command()
def list_sources():
    """Показать доступные источники."""
    from .config import load_sources
    for k, v in load_sources().items():
        click.echo(f"{k} → {v['adapter']} ({len(v['targets'])} targets)")

@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True)
@click.option("--reload", is_flag=True, default=False)
def serve(host: str, port: int, reload: bool):
    """Запустить веб-интерфейс."""
    import uvicorn
    click.echo(f"Интерфейс: http://{host}:{port}")
    uvicorn.run(
        "bank_audit.web.app:app",
        host=host, port=port,
        reload=reload,
        log_level="warning",
    )

if __name__ == "__main__":
    cli()

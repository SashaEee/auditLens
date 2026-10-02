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
@click.option("--limit", default=120, help="сколько офферов обогатить за прогон")
@click.option("--category", "categories", multiple=True,
              help="ограничить категориями (по умолчанию карты и кредиты)")
@click.option("--force", is_flag=True,
              help="перечитать уже разобранное (после правки промпта или правил)")
def enrich(limit: int, categories: tuple[str, ...], force: bool):
    """Обогатить офферы структурой условий (LLM по детальным страницам)."""
    from .normalizer import enrich_llm
    res = enrich_llm.enrich(limit=limit, categories=categories or None, force=force)
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

@cli.command("agent-eval")
@click.option("--model", default=None, help="маршрут модели Hermes (oss, gpt54mini, sonnet…)")
@click.option("--cases", default=None, help="только эти кейсы через запятую: S1,P2")
@click.option("--no-judge", is_flag=True, help="без судьи-модели, только детерминированные проверки")
@click.option("--trigger", default="cli", help="метка запуска: cli | gate | schedule")
@click.option("--json", "as_json", is_flag=True, help="итог одной строкой JSON (для скриптов)")
@click.option("--engine", default="quick", type=click.Choice(["quick", "deep"]),
              help="quick — быстрый режим (Hermes), deep — отчёт (deep research)")
def agent_eval(model, cases, no_judge, trigger, as_json, engine):
    """Регрессионный набор ИИ-аналитика: вопросы по всем вкладкам с живым эталоном."""
    import asyncio
    from .ai.agent_eval import run_eval
    only = [c.strip() for c in cases.split(",")] if cases else None
    res = asyncio.run(run_eval(model=model, only=only, use_judge=not no_judge, trigger=trigger,
                               engine=engine))
    if as_json:
        click.echo(json.dumps({k: res[k] for k in ("run_id", "model", "score", "n_pass",
                                                   "n_partial", "n_fail", "median_s")},
                              ensure_ascii=False))
        return
    for c in res["cases"]:
        bad = "; ".join(x["check"] for x in c.get("checks") or [] if not x["ok"])
        jd = c.get("judge") or {}
        click.echo(f"{c['id']:<3} {c['verdict']:<8} {c.get('seconds') or 0:>6.1f}с "
                   f"судья={jd.get('score', '—')}  {c['title']}"
                   + (f"  ✗ {bad}" if bad else "") + (f"  ⚠ {c['error']}" if c.get("error") else ""))
    click.echo(f"итог: {res['score']} (зачёт {res['n_pass']}, частично {res['n_partial']}, "
               f"провал {res['n_fail']}), медиана {res['median_s']} с, прогон {res['run_id']}")


def main():
    """Точка входа консольной команды `auditlens` (pyproject: bank_audit.cli:main).
    Раньше её не было — команда падала с ImportError, работал только
    `python -m bank_audit.cli`."""
    cli()


if __name__ == "__main__":
    main()

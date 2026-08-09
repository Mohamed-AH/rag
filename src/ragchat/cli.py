"""Command-line interface (Typer).

Provides the operational entrypoints — ingest a corpus, ask a one-off question, or run
the API server — mirroring what the HTTP service exposes. Installed as the ``ragchat``
console script (see ``pyproject.toml``).
"""

from __future__ import annotations

import typer

from ragchat.config import get_settings
from ragchat.logging_config import configure_logging

app = typer.Typer(
    add_completion=False,
    help="Retrieval-augmented Q&A over a PostgreSQL + pgvector knowledge base.",
)

# The CLI is a single-tenant admin tool; it operates on one fixed session.
_CLI_SESSION_ID = "default"


@app.command()
def ingest(
    path: str = typer.Argument("content.md", help="File to ingest (.md/.txt/.pdf/.docx)."),
) -> None:
    """Ingest a file and (re)build the knowledge base and vector index."""
    configure_logging(get_settings().log_level)
    from pathlib import Path

    from ragchat.service import build_session_service

    file_path = Path(path)
    service = build_session_service(_CLI_SESSION_ID)
    result = service.ingest_upload(file_path.name, file_path.read_bytes())
    typer.secho(
        f"Ingested {result.sections_written} sections from {path}.",
        fg=typer.colors.GREEN,
    )


@app.command()
def ask(
    question: str = typer.Argument(..., help="The question to answer."),
    show_sources: bool = typer.Option(True, help="Print the supporting source snippets."),
) -> None:
    """Ask a single question and print the answer (and optionally its sources)."""
    configure_logging(get_settings().log_level)
    from ragchat.service import build_session_service

    service = build_session_service(_CLI_SESSION_ID)
    result = service.ask(question)

    typer.secho("\nAnswer:", fg=typer.colors.CYAN, bold=True)
    typer.echo(result.answer)

    if show_sources and result.sources:
        typer.secho("\nSources:", fg=typer.colors.CYAN, bold=True)
        for source in result.sources:
            typer.echo(f"- {source.content[:200]}...")


@app.command()
def cleanup() -> None:
    """Purge sessions whose retention window has elapsed (run on a schedule)."""
    configure_logging(get_settings().log_level)
    from ragchat.service import purge_expired_sessions

    purged = purge_expired_sessions()
    typer.secho(f"Purged {purged} expired sessions.", fg=typer.colors.GREEN)


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="Bind host."),
    port: int = typer.Option(8000, help="Bind port."),
    reload: bool = typer.Option(False, help="Enable auto-reload (development only)."),
) -> None:
    """Run the FastAPI server with uvicorn."""
    import uvicorn

    uvicorn.run("ragchat.api.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":  # pragma: no cover
    app()

# === FILE: seance_runner.py ===================================================
from __future__ import annotations

from pathlib import Path
import os
import typer

from geist_agent.utils import ReportUtils, walk_files_compat as walk_files

from .seance_index import (
    connect as seance_connect,
    build_index as seance_build_index,
    load_manifest, index_path, seance_dir
)
from .seance_query import retrieve, generate_answer
from .seance_session import SeanceSession

app = typer.Typer(help="Ask questions about your codebase (or any supported text files).")

# ─────────────────────────────────── connect ───────────────────────────────────

@app.command("connect")
def connect(
    path: str = typer.Option(".", help="Root path of the filebase"),
    name: str = typer.Option(None, help="Seance name (defaults to generated name)"),
):
    """
    Initialize a seance under .geist/seance/<name>/ without indexing yet.
    """
    root = Path(path).resolve()
    if name is None:
        name = ReportUtils.generate_filename(topic=root.name)
    typer.secho(f"🔮 Connecting to: {root}", fg="cyan")
    seance_connect(root, name)
    out = root / ".geist" / "seance" / name
    typer.secho(f"• Seance created: {os.fspath(out)}", fg="green")
    typer.secho("Next: run `poltergeist seance index`", fg="yellow")

# ─────────────────────────────────── index ─────────────────────────────────────

@app.command("index")
def index(
    path: str = typer.Option(".", help="Root path of the filebase"),
    name: str = typer.Option(None, help="Seance name (defaults to generated name)"),
    max_chars: int = typer.Option(1200, help="Max chars per chunk"),
    overlap: int = typer.Option(150, help="Chunk overlap (in chars; converted to ~lines)"),
):
    """
    Build or update the seance index incrementally.
    """
    root = Path(path).resolve()
    if name is None:
        name = ReportUtils.generate_filename(topic=root.name)
    typer.secho(f"🧭 Indexing: {root}", fg="cyan")
    seance_build_index(root, name, max_chars=max_chars, overlap=overlap, verbose=True)
    out = root / ".geist" / "seance" / name / "inverted_index.json"
    typer.secho(f'🪵 Index ready: "{os.fspath(out)}"', fg="green")

# ───────────────────────────────────── ask ─────────────────────────────────────

@app.command("ask")
def ask(
    question: str = typer.Argument(..., help="Your question about the filebase"),
    path: str = typer.Option(".", help="Root path of the filebase"),
    name: str = typer.Option(None, help="Seance name (defaults to generated name)"),
    k: int = typer.Option(6, help="How many chunks to retrieve"),
    show_sources: bool = typer.Option(True, help="Show file:line ranges"),
):
    """
    Retrieve top context chunks and answer (non-LLM baseline, with citations).
    """
    root = Path(path).resolve()
    if name is None:
        name = ReportUtils.generate_filename(topic=root.name)
    typer.secho(f"🔎 Asking: “{question}”", fg="cyan")
    matches = retrieve(root, name, question, k=k)

    man = load_manifest(root, name)
    if not man:
        raise typer.Exit(code=1)

    contexts = []
    sources_out = []
    for cid, _score in matches:
        meta = man.chunks.get(cid)
        if not meta:
            continue
        fp = root / meta.file
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
            slice_ = lines[meta.start_line - 1: meta.end_line]
            preview = "\n".join(slice_)
        except Exception:
            preview = "(unreadable chunk)"
        contexts.append((cid, meta.file, meta.start_line, meta.end_line, preview))
        sources_out.append(f"{meta.file}:{meta.start_line}-{meta.end_line}")

    answer = generate_answer(question, contexts)

    typer.echo()
    typer.secho("━━━━━━━━━━━━━━━━ SÉANCE ━━━━━━━━━━━━━━━━", fg="magenta")
    typer.echo(answer)
    typer.secho("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", fg="magenta")
    if show_sources:
        typer.echo()
        typer.secho("Sources:", fg="yellow")
        for s in sources_out:
            typer.echo(f"  • {s}")

# ───────────────────────────────────── chat ────────────────────────────────────

@app.command("chat")
def chat(
    path: str = typer.Option(".", help="Root path of the filebase"),
    name: str = typer.Option(None, help="Seance name (defaults to generated name)"),
    k: int = typer.Option(6, help="How many chunks to retrieve per turn"),
    show_sources: bool = typer.Option(True, help="Show file:line ranges under each answer"),
):
    """
    Interactive chat loop tied to a specific seance index.
    Transcripts are written under .geist/seance/<name>/sessions/<timestamp>_<slug>/
    """
    root = Path(path).resolve()
    if name is None:
        name = ReportUtils.generate_filename(topic=root.name)

    typer.secho(f"🔮 Opening séance on: {root}", fg="cyan")
    man = load_manifest(root, name)
    if not man:
        typer.secho("No manifest found. Run:", fg="red")
        typer.echo(f"  poltergeist seance connect --path {os.fspath(root)} --name {name}")
        typer.echo(f"  poltergeist seance index --name {name}")
        raise typer.Exit(code=1)

    ip = index_path(root, name)
    if not ip.exists():
        typer.secho("No index found. Run:", fg="red")
        typer.echo(f"  poltergeist seance index --name {name}")
        raise typer.Exit(code=1)

    sdir = seance_dir(root, name)
    session = SeanceSession(sdir, name=name, slug=name, k=k, show_sources=show_sources)

    typer.secho("• Connected to index.", fg="green")
    paths = session.paths
    typer.secho(f"• Session folder: {paths['folder']}", fg="yellow")
    typer.secho("Type your questions. Commands: :help, :q, :k <n>, :sources on|off, :show session", fg="cyan")
    typer.echo("")

    # Warm welcome (logged)
    welcome = "Séance is listening. Ask about this codebase (or supported text files)."
    session.append_message("system", welcome)
    typer.secho(welcome, fg="magenta")

    while True:
        try:
            typer.echo("")  # spacer
            question = typer.prompt("you")
        except (KeyboardInterrupt, EOFError):
            typer.secho("\n(Interrupted)", fg="red")
            break

        # Commands
        if question.strip().startswith(":"):
            _handle_repl_command(question.strip(), session)
            continue

        if not question.strip():
            continue

        # Log user message
        session.append_message("user", question)

        # Retrieve
        typer.secho("⋯ retrieving context …", fg="blue")
        matches = retrieve(root, name, question, k=session.info.k)

        # Build contexts + sources
        man = load_manifest(root, name)  # cheap refresh
        contexts = []
        sources_out = []
        for cid, _score in matches:
            meta = man.chunks.get(cid)
            if not meta:
                continue
            fp = root / meta.file
            try:
                lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                slice_ = lines[meta.start_line - 1: meta.end_line]
                preview = "\n".join(slice_)
            except Exception:
                preview = "(unreadable chunk)"
            contexts.append((cid, meta.file, meta.start_line, meta.end_line, preview))
            sources_out.append(f"{meta.file}:{meta.start_line}-{meta.end_line}")

        # Synthesize non-LLM baseline
        answer = generate_answer(question, contexts)

        # Print + log
        typer.echo("")
        typer.secho("━━━━━━━━━━━━ RESPONSE ━━━━━━━━━━━━", fg="magenta")
        typer.echo(answer)
        typer.secho("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", fg="magenta")

        meta = {"sources": sources_out} if session.info.show_sources else {}
        session.append_message("assistant", answer, meta=meta)

    typer.secho("\nSession closed. Transcript saved.", fg="green")
    typer.secho(f"  • {paths['transcript']}", fg="yellow")
    typer.secho(f"  • {paths['messages']}", fg="yellow")


def _handle_repl_command(cmd: str, session: SeanceSession):
    parts = cmd.split()
    if parts[0] in (":q", ":quit", ":exit"):
        raise typer.Exit(code=0)
    if parts[0] == ":help":
        typer.secho("Commands:", fg="yellow")
        typer.echo("  :q                      Quit")
        typer.echo("  :k <n>                 Set top-k retrieval")
        typer.echo("  :sources on|off        Toggle source printing")
        typer.echo("  :show session          Print session folder paths")
        return
    if parts[0] == ":k":
        if len(parts) >= 2 and parts[1].isdigit():
            session.set_k(int(parts[1]))
            typer.secho(f"• k set to {session.info.k}", fg="green")
        else:
            typer.secho("Usage: :k 8", fg="red")
        return
    if parts[0] == ":sources":
        if len(parts) >= 2 and parts[1] in ("on", "off"):
            val = parts[1] == "on"
            session.set_show_sources(val)
            typer.secho(f"• show_sources = {val}", fg="green")
        else:
            typer.secho("Usage: :sources on|off", fg="red")
        return
    if parts[0] == ":show" and len(parts) >= 2 and parts[1] == "session":
        p = session.paths
        typer.secho("Session paths:", fg="yellow")
        for k, v in p.items():
            typer.echo(f"  {k:10}: {v}")
        return

    typer.secho(f"Unknown command: {cmd}", fg="red")

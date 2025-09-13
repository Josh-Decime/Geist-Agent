# === FILE: src/geist_agent/seance/seance_runner.py =============================
from __future__ import annotations

from pathlib import Path
import os
import re
import typer

import sys
import threading
import time
from contextlib import contextmanager

from geist_agent.utils import ReportUtils, walk_files_compat as walk_files

from .seance_index import (
    connect as seance_connect,
    build_index as seance_build_index,
    load_manifest, index_path, seance_dir
)
from .seance_query import retrieve, generate_answer
from .seance_session import SeanceSession

app = typer.Typer(help="Ask questions about your codebase (or any supported text files).")

@contextmanager
def _spinner(label: str):
    """Minimal CLI spinner; use only when not in verbose mode."""
    stop = False
    def run():
        glyphs = "|/-\\"
        i = 0
        while not stop:
            sys.stdout.write("\r" + label + " " + glyphs[i % len(glyphs)])
            sys.stdout.flush()
            time.sleep(0.1)
            i += 1
        sys.stdout.write("\r" + " " * (len(label) + 2) + "\r")
        sys.stdout.flush()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop = True
        t.join()

# ---------- helpers ------------------------------------------------------------
def _default_seance_name(root: Path) -> str:
    """Stable default: current folder name as a slug."""
    s = root.name.strip().lower().replace(" ", "_")
    s = re.sub(r"[^a-z0-9._-]+", "", s)
    return s or "seance"

# ─────────────────────────────────── connect ───────────────────────────────────
@app.command("connect")
def connect(
    path: str = typer.Option(".", help="Root path of the filebase"),
    name: str = typer.Option(None, help="Seance name (defaults to folder name)"),
):
    root = Path(path).resolve()
    if name is None:
        name = _default_seance_name(root)
    typer.secho(f"🔮 Connecting to: {root}", fg="cyan")
    seance_connect(root, name)
    out = seance_dir(root, name)
    typer.secho(f"• Seance created: {os.fspath(out)}", fg="green")
    typer.secho("Next: run `poltergeist seance index` (or just `poltergeist seance` to auto-index+chat)", fg="yellow")

# ─────────────────────────────────── index ─────────────────────────────────────
@app.command("index")
def index(
    path: str = typer.Option(".", help="Root path of the filebase"),
    name: str = typer.Option(None, help="Seance name (defaults to folder name)"),
    max_chars: int = typer.Option(1200, help="Max chars per chunk"),
    overlap: int = typer.Option(150, help="Chunk overlap (chars; ~lines heuristic)"),
):
    root = Path(path).resolve()
    if name is None:
        name = _default_seance_name(root)
    typer.secho(f"🧭 Indexing: {root}", fg="cyan")
    seance_build_index(root, name, max_chars=max_chars, overlap=overlap, verbose=True)
    out = index_path(root, name)
    typer.secho(f'🪵 Index ready: "{os.fspath(out)}"', fg="green")

# ───────────────────────────────────── ask ─────────────────────────────────────
@app.command("ask")
def ask(
    question: str = typer.Argument(..., help="Your question about the filebase"),
    path: str = typer.Option(".", help="Root path of the filebase"),
    name: str = typer.Option(None, help="Seance name (defaults to folder name)"),
    k: int = typer.Option(6, help="How many chunks to retrieve"),
    show_sources: bool = typer.Option(True, help="Show file:line ranges"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Disable LLM; use extractive preview"),
    model: str = typer.Option(None, "--model", help="LLM model id (defaults from env)"),
    verbose: bool = typer.Option(False, "--verbose", help="Show detailed agent logs"),
):
    root = Path(path).resolve()
    if name is None:
        name = _default_seance_name(root)
    # if first-time use, bootstrap
    if not load_manifest(root, name) or not index_path(root, name).exists():
        typer.secho("• Bootstrapping seance (connect + index)…", fg="yellow")
        seance_connect(root, name)
        seance_build_index(root, name, verbose=True)

    typer.secho(f"🔎 Asking: “{question}”", fg="cyan")
    matches = retrieve(root, name, question, k=k)
    man = load_manifest(root, name)
    if not man:
        raise typer.Exit(code=1)

    contexts, sources_out = [], []
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
        
        model_display = model or os.getenv("SEANCE_MODEL") or os.getenv("MODEL") or "default-model"

        if verbose:
            answer, mode, reason = generate_answer(
                question, contexts, use_llm=not no_llm, model=model, verbose=True
            )
        else:
            with _spinner(f"LLM (model={model_display}) is thinking…"):
                answer, mode, reason = generate_answer(
                    question, contexts, use_llm=not no_llm, model=model, verbose=False
                )

    answer, mode, reason = generate_answer(
    question, contexts,
    use_llm=not no_llm,
    model=model,
    )

    typer.echo()
    typer.secho("━━━━━━━━━━━━━━━━ SÉANCE ━━━━━━━━━━━━━━━━", fg="magenta")
    typer.echo(answer)
    typer.secho("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", fg="magenta")

    # NEW: show LLM vs fallback status with reason/model
    typer.secho(
        f"• Answer mode: {'LLM' if mode=='llm' else 'fallback'}"
        + (f" (model={model or os.getenv('GEIST_SEANCE_OPENAI_MODEL','gpt-4o-mini')})" if mode=='llm' else (f" — {reason}" if reason else "")),
        fg=("green" if mode == "llm" else "yellow"),
    )

    if show_sources:
        typer.echo()
        typer.secho("Sources:", fg="yellow")
        for s in sources_out:
            typer.echo(f"  • {s}")


# ───────────────────────────────────── chat ────────────────────────────────────
@app.command("chat")
def chat(
    path: str = typer.Option(".", help="Root path of the filebase"),
    name: str = typer.Option(None, help="Seance name (defaults to folder name)"),
    k: int = typer.Option(6, help="How many chunks to retrieve per turn"),
    show_sources: bool = typer.Option(True, help="Show file:line ranges under each answer"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Disable LLM; use extractive preview"),
    model: str = typer.Option(None, "--model", help="LLM model id (defaults from env)"),
    verbose: bool = typer.Option(False, "--verbose", help="Show detailed agent logs"),

):

    """
    Zero-step UX: if this is the first run, we'll connect + index for you, then chat.
    Transcripts + index live under ~/.geist/reports/seance/<name>/
    """
    root = Path(path).resolve()
    if name is None:
        name = _default_seance_name(root)

    # Bootstrap if needed
    need_connect = not load_manifest(root, name)
    need_index = not index_path(root, name).exists()
    if need_connect or need_index:
        typer.secho("• Bootstrapping seance (connect + index)…", fg="yellow")
        seance_connect(root, name)
        seance_build_index(root, name, verbose=True)

    sdir = seance_dir(root, name)
    session = SeanceSession(sdir, name=name, slug=name, k=k, show_sources=show_sources)

    typer.secho("• Connected to index.", fg="green")
    paths = session.paths
    typer.secho(f"• Session folder: {paths['folder']}", fg="yellow")
    typer.secho("Type your questions. Commands: :help, :q, :k <n>, :sources on|off, :show session", fg="cyan")
    typer.echo("")
    session.append_message("system", "Séance is listening. Ask about this codebase (or supported text files).")
    typer.secho("Séance is listening. Ask about this codebase (or supported text files).", fg="magenta")

    while True:
        try:
            typer.echo("")
            question = typer.prompt("you")
        except (KeyboardInterrupt, EOFError):
            typer.secho("\n(Interrupted)", fg="red")
            break

        if question.strip().startswith(":"):
            _handle_repl_command(question.strip(), session)
            continue
        if not question.strip():
            continue

        session.append_message("user", question)
        typer.secho("⋯ retrieving context …", fg="blue")
        matches = retrieve(root, name, question, k=session.info.k)

        man = load_manifest(root, name)  # refresh
        contexts, sources_out = [], []
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

        model_display = model or os.getenv("SEANCE_MODEL") or os.getenv("MODEL") or "default-model"

        if verbose:
            answer, mode, reason = generate_answer(
                question, contexts, use_llm=not no_llm, model=model, verbose=True
            )
        else:
            with _spinner(f"LLM (model={model_display}) is thinking…"):
                answer, mode, reason = generate_answer(
                    question, contexts, use_llm=not no_llm, model=model, verbose=False
                )

        answer, mode, reason = generate_answer(
            question, contexts,
            use_llm=not no_llm,
            model=model,
        )

        typer.echo("")
        typer.secho("━━━━━━━━━━━━ RESPONSE ━━━━━━━━━━━━", fg="magenta")
        typer.echo(answer)
        typer.secho("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", fg="magenta")

        # NEW: show LLM vs fallback status with reason/model
        typer.secho(
            f"• Answer mode: {'LLM' if mode=='llm' else 'fallback'}"
            + (f" (model={model or os.getenv('GEIST_SEANCE_OPENAI_MODEL','gpt-4o-mini')})" if mode=='llm' else (f" — {reason}" if reason else "")),
            fg=("green" if mode == "llm" else "yellow"),
        )

        session.append_message("assistant", answer, meta={"sources": sources_out} if session.info.show_sources else {})

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

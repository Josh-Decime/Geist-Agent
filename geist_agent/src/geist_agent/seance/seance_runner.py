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

# -------- env controls --------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except Exception:
        return default

def _env_bool(name: str, default: bool) -> bool:
    v = (os.getenv(name, "") or "").strip().lower()
    if v in ("1", "true", "yes", "on"): return True
    if v in ("0", "false", "no", "off"): return False
    return default

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except Exception:
        return default

def _expand_to_deep_contexts(
    contexts: list[tuple[str, str, int, int, str]],
    root: Path,
    top_n_files: int,
) -> list[tuple[str, str, int, int, str]]:
    """
    Given chunk contexts, expand to whole-file contexts for the top-N unique files.
    """
    out: list[tuple[str, str, int, int, str]] = []
    seen = set()
    for _cid, file, _s, _e, _prev in contexts:
        if file in seen:
            continue
        seen.add(file)
        fp = root / file
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
            total_lines = len(text.splitlines())
            out.append(("whole:" + file, file, 1, total_lines, text))
        except Exception:
            out.append(("whole:" + file, file, 1, 1, "(unreadable file)"))
        if len(out) >= top_n_files:
            break
    return out

# --------- loading indicator --------
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
    if max_chars == 1200:  # only replace when user used default
        max_chars = _env_int("SEANCE_MAX_CHARS", 1200)
    if overlap == 150:
        overlap = _env_int("SEANCE_OVERLAP", 150)
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
    deep: bool = typer.Option(False, "--deep", help="Feed whole files (top hits) to the LLM"),
):
    root = Path(path).resolve()
    if name is None:
        name = _default_seance_name(root)
    # if first-time use, bootstrap
    if not load_manifest(root, name) or not index_path(root, name).exists():
        typer.secho("• Bootstrapping seance (connect + index)…", fg="yellow")
        seance_connect(root, name)
        seance_build_index(root, name, verbose=True)

    # Allow .env to override defaults when the user left the defaults in place
    if k == 6:
        k = _env_int("SEANCE_DEFAULT_K", 6)

    retriever = (os.getenv("SEANCE_RETRIEVER") or "bm25").strip().lower()
    retriever = "bm25" if retriever not in ("bm25", "jaccard") else retriever

    # Candidate widening:
    # - non-deep: widen by SEANCE_WIDEN (default 2) so we can diversify by file
    # - deep: widen by SEANCE_DEEP_MULT (default 4) before expanding to whole files
    widen = _env_int("SEANCE_WIDEN", 2)            # non-deep: retrieve k*widen chunks
    deep_mult = _env_int("SEANCE_DEEP_MULT", 4)    # deep: retrieve k*deep_mult chunks
    retrieve_k = (k * deep_mult) if deep else (k * widen)

    typer.secho(f"🔎 Asking: “{question}”", fg="cyan")
    matches = retrieve(root, name, question, k=retrieve_k)
    man = load_manifest(root, name)
    if not man:
        raise typer.Exit(code=1)

    # Build contexts
    contexts, sources_out = [], []
    if deep:
        # Expand to whole files for the top-N unique files across the wider hit list
        top_n_files = _env_int("SEANCE_DEEP_TOP_FILES", 3)
        tmp = []
        seen_files = set()
        for cid, _ in matches:
            meta = man.chunks.get(cid)
            if not meta:
                continue
            if meta.file in seen_files:
                continue
            seen_files.add(meta.file)
            tmp.append((cid, meta.file, meta.start_line, meta.end_line, ""))  # placeholder preview
            if len(tmp) >= top_n_files:
                break
        contexts = _expand_to_deep_contexts(tmp, root, top_n_files)
        sources_out = [f"{file}:1-{end}" for (_cid, file, _s, end, _txt) in contexts]
    else:
        # Non-deep: diversify by file (on by default) and ensure we hit at least a minimum of unique files
        diversify = _env_bool("SEANCE_DIVERSIFY_FILES", True)
        min_unique = _env_int("SEANCE_MIN_UNIQUE_FILES", max(1, min(5, k)))  # try to give several files

        seen_files = set()
        for cid, _score in matches:
            meta = man.chunks.get(cid)
            if not meta:
                continue
            if diversify and meta.file in seen_files:
                continue
            seen_files.add(meta.file)
            fp = root / meta.file
            try:
                lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                slice_ = lines[meta.start_line - 1: meta.end_line]
                preview = "\n".join(slice_)
            except Exception:
                preview = "(unreadable chunk)"
            contexts.append((cid, meta.file, meta.start_line, meta.end_line, preview))
            sources_out.append(f"{meta.file}:{meta.start_line}-{meta.end_line}")
            # stop when we have k contexts AND we've satisfied the uniqueness minimum
            if len(contexts) >= k and len(seen_files) >= min_unique:
                break

        # If we still didn't reach min_unique (e.g., matches were dominated by one file),
        # take additional chunks from new files further down the list:
        if len(seen_files) < min_unique:
            for cid, _score in matches:
                if len(seen_files) >= min_unique or len(contexts) >= k:
                    break
                meta = man.chunks.get(cid)
                if not meta or (diversify and meta.file in seen_files):
                    continue
                seen_files.add(meta.file)
                fp = root / meta.file
                try:
                    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                    slice_ = lines[meta.start_line - 1: meta.end_line]
                    preview = "\n".join(slice_)
                except Exception:
                    preview = "(unreadable chunk)"
                contexts.append((cid, meta.file, meta.start_line, meta.end_line, preview))
                sources_out.append(f"{meta.file}:{meta.start_line}-{meta.end_line}")

    # --- retrieval feedback (shows in terminal before the spinner) ------------
    if _env_bool("SEANCE_RETRIEVAL_LOG", True):
        unique_files = len({f for (_cid, f, _s, _e, _txt) in contexts})
        typer.secho(
            f"• Retrieval: {('DEEP' if deep else retriever.upper())} "
            f"| hits={len(matches)} | contexts={len(contexts)} | files={unique_files}",
            fg="blue",
        )

    # Thinking indicator shows which retrieval mode was used
    mode_label = "DEEP" if deep else retriever.upper()
    model_display = model or os.getenv("SEANCE_MODEL") or os.getenv("MODEL") or "default-model"

    if verbose:
        answer, mode, reason = generate_answer(
            question, contexts, use_llm=not no_llm, model=model, verbose=True
        )
    else:
        with _spinner(f"{mode_label} | LLM (model={model_display}) is thinking…"):
            answer, mode, reason = generate_answer(
                question, contexts, use_llm=not no_llm, model=model, verbose=False
            )

    typer.echo()
    typer.secho("━━━━━━━━━━━━━━━━ SÉANCE ━━━━━━━━━━━━━━━━", fg="magenta")
    typer.echo(answer)
    typer.secho("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", fg="magenta")

    typer.secho(
        f"• Answer mode: {'LLM' if mode=='llm' else 'fallback'}"
        + (f" (model={model_display})" if mode=='llm' else (f" — {reason}" if reason else "")),
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
    deep: bool = typer.Option(False, "--deep", help="Feed whole files (top hits) to the LLM"),

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

    # If user left default k=6, allow .env override: SEANCE_DEFAULT_K
    if k == 6:
        k = _env_int("SEANCE_DEFAULT_K", 6)

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

        # --- inline --deep toggle per question --------------------------------
        q_tokens = question.split()
        q_deep = "--deep" in q_tokens
        if q_deep:
            q_tokens = [t for t in q_tokens if t != "--deep"]
            question = " ".join(q_tokens).strip()
            # If the user typed only --deep, treat it like a toggle and skip this turn
            if not question:
                session.meta = getattr(session, "meta", {})
                session.meta["deep"] = True
                typer.secho("• deep = True (will apply to next question)", fg="green")
                continue


        session.append_message("user", question)
        typer.secho("⋯ retrieving context …", fg="blue")

        # Decide whether deep is in effect this turn:
        # 1) CLI flag --deep
        # 2) REPL toggle :deep on|off (session.meta["deep"])
        # 3) inline --deep for this message
        use_deep = deep
        if hasattr(session, "meta") and isinstance(session.meta, dict):
            use_deep = session.meta.get("deep", use_deep)
        if q_deep:
            use_deep = True

        # Which retriever?
        retriever = (os.getenv("SEANCE_RETRIEVER") or "bm25").strip().lower()
        retriever = "bm25" if retriever not in ("bm25", "jaccard") else retriever

        # Candidate widening
        widen = _env_int("SEANCE_WIDEN", 2)           # non-deep widening
        deep_mult = _env_int("SEANCE_DEEP_MULT", 4)   # deep widening
        retrieve_k = (session.info.k * deep_mult) if use_deep else (session.info.k * widen)

        matches = retrieve(root, name, question, k=retrieve_k)


        man = load_manifest(root, name)  # refresh
        contexts, sources_out = [], []

        if use_deep:
            # Top-N unique files from the wider candidate pool
            top_n = _env_int("SEANCE_DEEP_TOP_FILES", 3)
            tmp = []
            seen_files = set()
            for cid, _ in matches:
                meta = man.chunks.get(cid)
                if not meta:
                    continue
                if meta.file in seen_files:
                    continue
                seen_files.add(meta.file)
                tmp.append((cid, meta.file, meta.start_line, meta.end_line, ""))  # placeholder
                if len(tmp) >= top_n:
                    break
            contexts = _expand_to_deep_contexts(tmp, root, top_n)
            sources_out = [f"{file}:1-{end}" for (_cid, file, _s, end, _txt) in contexts]
        else:
            diversify = _env_bool("SEANCE_DIVERSIFY_FILES", True)
            min_unique = _env_int("SEANCE_MIN_UNIQUE_FILES", max(1, min(5, session.info.k)))

            seen_files = set()
            for cid, _score in matches:
                meta = man.chunks.get(cid)
                if not meta:
                    continue
                if diversify and meta.file in seen_files:
                    continue
                seen_files.add(meta.file)
                fp = root / meta.file
                try:
                    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                    slice_ = lines[meta.start_line - 1: meta.end_line]
                    preview = "\n".join(slice_)
                except Exception:
                    preview = "(unreadable chunk)"
                contexts.append((cid, meta.file, meta.start_line, meta.end_line, preview))
                sources_out.append(f"{meta.file}:{meta.start_line}-{meta.end_line}")
                # stop when we have k contexts AND we've hit the uniqueness minimum
                if len(contexts) >= session.info.k and len(seen_files) >= min_unique:
                    break

            # If we didn't reach min_unique, sweep again to grab new files farther down:
            if len(seen_files) < min_unique:
                for cid, _score in matches:
                    if len(seen_files) >= min_unique or len(contexts) >= session.info.k:
                        break
                    meta = man.chunks.get(cid)
                    if not meta or (diversify and meta.file in seen_files):
                        continue
                    seen_files.add(meta.file)
                    fp = root / meta.file
                    try:
                        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                        slice_ = lines[meta.start_line - 1: meta.end_line]
                        preview = "\n".join(slice_)
                    except Exception:
                        preview = "(unreadable chunk)"
                    contexts.append((cid, meta.file, meta.start_line, meta.end_line, preview))
                    sources_out.append(f"{meta.file}:{meta.start_line}-{meta.end_line}")


        # Spinner shows which retrieval path ran
        mode_label = "DEEP" if use_deep else retriever.upper()
        model_display = model or os.getenv("SEANCE_MODEL") or os.getenv("MODEL") or "default-model"

        # --- retrieval feedback (shows in terminal before the spinner) --------
        if _env_bool("SEANCE_RETRIEVAL_LOG", True):
            unique_files = len({f for (_cid, f, _s, _e, _txt) in contexts})
            typer.secho(
                f"• Retrieval: {mode_label} | hits={len(matches)} | contexts={len(contexts)} | files={unique_files}",
                fg="blue",
            )

        if verbose:
            answer, mode, reason = generate_answer(
                question, contexts, use_llm=not no_llm, model=model, verbose=True
            )
        else:
            with _spinner(f"{mode_label} | LLM (model={model_display}) is thinking…"):
                answer, mode, reason = generate_answer(
                    question, contexts, use_llm=not no_llm, model=model, verbose=False
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
        typer.echo("  :deep on|off           Toggle deep (whole-file) context")
        typer.echo("  :show session          Print session folder paths")
        return
    
    if parts[0] == ":deep":
        if len(parts) >= 2 and parts[1] in ("on", "off"):
            val = parts[1] == "on"
            session.meta = getattr(session, "meta", {})
            session.meta["deep"] = val
            typer.secho(f"• deep = {val}", fg="green")
        else:
            typer.secho("Usage: :deep on|off", fg="red")
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

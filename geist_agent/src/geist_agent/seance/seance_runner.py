# src/geist_agent/seance/seance_runner.py
from __future__ import annotations

import os
import re
import typer
import sys
import threading
import time
import io
from pathlib import Path
from contextlib import contextmanager
from geist_agent.utils import EnvUtils
from .seance_index import (
    connect as seance_connect,
    build_index as seance_build_index,
    load_manifest, index_path, seance_dir
)
from .seance_query import retrieve, generate_answer
from .seance_session import SeanceSession

app = typer.Typer(help="Ask questions about your codebase (or any supported text files).")

# --- Windows console ANSI fix (safe no-op on non-Windows) ---
try:
    import colorama
    colorama.just_fix_windows_console()
except Exception:
    pass

# --- optional ANSI stripper for terminal echo (we'll keep transcript clean in SeanceSession) ---
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

def _strip_ansi(s: str) -> str:
    if not isinstance(s, str):
        return ""
    return _ANSI_RE.sub("", s)

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

def _expand_to_deep_contexts(
    contexts: list[tuple[str, str, int, int, str]],
    root: Path,
    top_n_files: int,
) -> list[tuple[str, str, int, int, str]]:
    """
    Build deep contexts by expanding around the *most relevant chunk window*
    for each top file, instead of dumping entire files.

    Env knobs:
      SEANCE_DEEP_WINDOW_LINES   (default 120)    -> +/- lines around best chunk
      SEANCE_DEEP_MAX_FILE_CHARS (default 4000)   -> cap text per file
      SEANCE_DEEP_MIN_OVERLAP    (default 1)      -> skip if window barely overlaps
    """
    def _tokenize(s: str) -> set[str]:
        return set(re.findall(r"[A-Za-z0-9_]+", s.lower()))

    # read env knobs (fail-safe defaults)
    try:
        window_lines = int(os.getenv("SEANCE_DEEP_WINDOW_LINES", "120"))
    except Exception:
        window_lines = 120
    try:
        max_file_chars = int(os.getenv("SEANCE_DEEP_MAX_FILE_CHARS", "4000"))
    except Exception:
        max_file_chars = 4000
    try:
        min_overlap = int(os.getenv("SEANCE_DEEP_MIN_OVERLAP", "1"))
    except Exception:
        min_overlap = 1

    out: list[tuple[str, str, int, int, str]] = []
    seen_files: set[str] = set()

    for (_cid, file, s, e, _prev) in contexts:
        if file in seen_files:
            continue
        seen_files.add(file)

        fp = root / file
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            out.append((f"win:{file}", file, 1, 1, "(unreadable file)"))
            if len(out) >= top_n_files:
                break
            continue

        total = len(lines)
        # initial window centered on best chunk span
        start = max(1, s - window_lines)
        end   = min(total, e + window_lines)
        text  = "\n".join(lines[start - 1:end])

        # cap by character budget for this file
        if len(text) > max_file_chars:
            text = text[:max_file_chars]

        # lightweight overlap gate: does window actually relate to the chunk?
        chunk_tokens = _tokenize("\n".join(lines[s - 1:e]))
        win_tokens   = _tokenize(text)
        if len(chunk_tokens & win_tokens) < min_overlap:
            # try one wider pass
            alt_start = max(1, start - window_lines // 2)
            alt_end   = min(total, end + window_lines // 2)
            alt_text  = "\n".join(lines[alt_start - 1:alt_end])
            if len(alt_text) > max_file_chars:
                alt_text = alt_text[:max_file_chars]
            if len(chunk_tokens & _tokenize(alt_text)) >= min_overlap:
                start, end, text = alt_start, alt_end, alt_text
            else:
                # still too weak; skip this file
                continue

        out.append((f"win:{file}", file, start, end, text))
        if len(out) >= top_n_files:
            break

    return out

def _expand_to_wide_contexts(
    matches: list[tuple[str, float]],
    man,
    root: Path,
    top_n_files: int,
    window_lines: int,
    max_chars: int,
) -> list[tuple[str, str, int, int, str]]:
    """
    Pick the best chunk per top-N files and take a SMALL window around it.
    Goal: breadth across many files with tiny per-file snippets.
    """
    # 1) keep highest-scoring chunk per file + file totals
    file_best: dict[str, tuple[str, int, int, float]] = {}
    file_tot: dict[str, float] = {}

    for cid, score in matches:
        meta = man.chunks.get(cid)
        if not meta:
            continue
        file_tot[meta.file] = file_tot.get(meta.file, 0.0) + float(score)
        prev = file_best.get(meta.file)
        if prev is None or score > prev[3]:
            file_best[meta.file] = (cid, meta.start_line, meta.end_line, float(score))

    # 2) choose top-N files by total relevance
    ranked = sorted(file_tot.items(), key=lambda kv: kv[1], reverse=True)[:top_n_files]

    # 3) build tiny windows
    out: list[tuple[str, str, int, int, str]] = []
    for file, _tot in ranked:
        cid, s, e, _best = file_best[file]
        fp = root / file
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            out.append((cid, file, 1, 1, "(unreadable file)"))
            continue

        total = len(lines)
        start = max(1, min(s, e) - window_lines)
        end   = min(total, max(s, e) + window_lines)
        text  = "\n".join(lines[start - 1:end])
        if len(text) > max_chars:
            text = text[:max_chars]

        out.append((cid, file, start, end, text))

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

# --------- tee stdout (print live and capture) --------
@contextmanager
def _tee_stdout():
    """
    Duplicate stdout to both the terminal and a buffer so --verbose logs
    appear live AND are captured for the transcript.

    If SEANCE_STRIP_ANSI_IN_TERMINAL is truthy, strip ANSI for the terminal
    while keeping the raw text in the buffer.
    """
    old = sys.stdout
    buf = io.StringIO()
    strip_for_terminal = os.getenv("SEANCE_STRIP_ANSI_IN_TERMINAL", "").strip().lower() in ("1", "true", "yes", "on")

    class _Tee(io.TextIOBase):
        def write(self, s):
            # live echo to terminal
            try:
                old.write(_strip_ansi(s) if strip_for_terminal else s)
                old.flush()
            except Exception:
                pass
            # always keep raw in buffer
            buf.write(s)
            return len(s)
        def flush(self):
            try:
                old.flush()
            except Exception:
                pass
            buf.flush()

    sys.stdout = _Tee()
    try:
        yield buf
    finally:
        sys.stdout = old

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
    env_reload: bool = typer.Option(False, "--env", help="Reload .env before answering"),
    #Remove when --wide is rebuilt
    wide: bool = typer.Option(False, "--wide", help="Breadth-first context: tiny snippets from many files"),

):

    """
    Zero-step UX: if this is the first run, we'll connect + index for you, then chat.
    Transcripts + index live under ~/.geist/reports/seance/<name>/
    """
    root = Path(path).resolve()
    if name is None:
        name = _default_seance_name(root)

    # Always (re)connect + (re)index once when chat starts
    typer.secho("• Preparing index (connect + index)…", fg="yellow")
    seance_connect(root, name)
    idx_verbose = _env_bool("SEANCE_INDEX_VERBOSE", True)
    max_chars_env = _env_int("SEANCE_MAX_CHARS", 1200)
    overlap_env   = _env_int("SEANCE_OVERLAP", 150)
    seance_build_index(root, name, max_chars=max_chars_env, overlap=overlap_env, verbose=idx_verbose)


    # If user left default k=6, allow .env override: SEANCE_DEFAULT_K
    if k == 6:
        k = _env_int("SEANCE_DEFAULT_K", 6)

    sdir = seance_dir(root, name)
    session = SeanceSession(sdir, name=name, slug=name, k=k, show_sources=show_sources)

    typer.secho("• Connected to index.", fg="green")
    paths = session.paths
    typer.secho(f"• Session folder: {paths['folder']}", fg="yellow")
    typer.secho("Type your questions. Commands: :help, :q, :k <n>, :sources on|off, :deep on|off, :wide on|off, :verbose on|off, :env, :show session", fg="cyan")
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

        # --- inline --deep / --verbose toggles per question --------------------
        q_tokens = question.split()
        q_deep = "--deep" in q_tokens
        q_verbose = "--verbose" in q_tokens
        q_env = "--env" in q_tokens
        q_wide = "--wide" in q_tokens

        if q_deep:
            q_tokens = [t for t in q_tokens if t != "--deep"]
        if q_verbose:
            q_tokens = [t for t in q_tokens if t != "--verbose"]
        if q_env:
            q_tokens = [t for t in q_tokens if t != "--env"]
        if q_wide:
            q_tokens = [t for t in q_tokens if t != "--wide"]

        question = " ".join(q_tokens).strip()

        # If the user typed only a toggle, set it for next turn and skip now
        if not question:
            session.meta = getattr(session, "meta", {})
            if q_deep:
                session.meta["deep"] = True
                typer.secho("• deep = True (will apply to next question)", fg="green")
            if q_verbose:
                session.meta["verbose"] = True
                typer.secho("• verbose = True (will apply to next question)", fg="green")
            if q_wide:
                session.meta["wide"] = True
                typer.secho("• wide = True (will apply to next question)", fg="green")
            if q_env:
                try:
                    loaded = EnvUtils.load_env_for_tool()
                    typer.secho(f"• env reloaded ({len(loaded)} sources)", fg="green")
                except Exception as e:
                    typer.secho(f"• env reload failed: {e}", fg="red")
            continue

        session.append_message("user", question)
        typer.secho("⋯ retrieving context …", fg="blue")

        # Decide whether deep/verbose are in effect this turn:
        # deep precedence: CLI flag -> REPL toggle -> inline flag
        use_deep = deep
        active_verbose = verbose
        use_wide = wide

        if hasattr(session, "meta") and isinstance(session.meta, dict):
            use_deep = session.meta.get("deep", use_deep)
            active_verbose = session.meta.get("verbose", active_verbose)
            use_wide = session.meta.get("wide", use_wide)

        if q_deep:
            use_deep = True
        if q_verbose:
            active_verbose = True
        if q_wide:
            use_wide = True

        # precedence: wide > deep
        if use_wide:
            use_deep = False

        if env_reload or q_env:
            try:
                loaded = EnvUtils.load_env_for_tool()
                typer.secho(f"• env reloaded ({len(loaded)} sources)", fg="green")
            except Exception as e:
                typer.secho(f"• env reload failed: {e}", fg="red")

        # Which retriever?
        retriever = (os.getenv("SEANCE_RETRIEVER") or "bm25").strip().lower()
        retriever = "bm25" if retriever not in ("bm25", "jaccard") else retriever

        # Candidate widening
        widen     = _env_int("SEANCE_WIDEN", 2)          # default mode multiplier
        deep_mult = _env_int("SEANCE_DEEP_MULT", 4)      # deep mode multiplier
        wide_mult = _env_int("SEANCE_WIDE_MULT", 6)      # wide mode multiplier

        if use_wide:
            retrieve_k = session.info.k * wide_mult
        elif use_deep:
            retrieve_k = session.info.k * deep_mult
        else:
            retrieve_k = session.info.k * widen

        matches = retrieve(root, name, question, k=retrieve_k)

        man = load_manifest(root, name)  # refresh
        contexts, sources_out = [], []

        if use_wide:
            # env knobs for wide
            top_n     = _env_int("SEANCE_WIDE_TOP_FILES", max(session.info.k, 10))
            win_lines = _env_int("SEANCE_WIDE_WINDOW_LINES", 10)   # tiny per-file slice
            max_chars = _env_int("SEANCE_WIDE_MAX_FILE_CHARS", 600)

            # matches is list[(cid, score)], pass to expander
            contexts = _expand_to_wide_contexts(
                matches, man, root,
                top_n_files=top_n,
                window_lines=win_lines,
                max_chars=max_chars,
            )
            sources_out = [f"{file}:{s}-{e}" for (_cid, file, s, e, _txt) in contexts]

        elif use_deep:
            # --- SEANCE DEEP: aggregate by file & expand best windows ---
            top_n = _env_int("SEANCE_DEEP_TOP_FILES", 3)

            # 1) sum candidate scores per file, and remember the best chunk per file
            file_scores: dict[str, float] = {}
            best_window: dict[str, tuple[str, int, int, float]] = {}

            for cid, score in matches:
                meta = man.chunks.get(cid)
                if not meta:
                    continue
                file_scores[meta.file] = file_scores.get(meta.file, 0.0) + float(score)
                prev = best_window.get(meta.file)
                if prev is None or score > prev[3]:
                    best_window[meta.file] = (cid, meta.start_line, meta.end_line, float(score))

            # 2) choose top-N files by total relevance
            ranked_files = sorted(file_scores.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

            # 3) prepare windows for expander (use each file's best chunk span)
            tmp = []
            for file, _tot in ranked_files:
                cid, s, e, _best = best_window[file]
                tmp.append((cid, file, s, e, ""))  # preview filled by expander

            # 4) expand those windows (bounded slices, not whole files)
            contexts = _expand_to_deep_contexts(tmp, root, top_n)
            sources_out = [f"{file}:{s}-{e}" for (_cid, file, s, e, _txt) in contexts]

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
        mode_label = "WIDE" if use_wide else ("DEEP" if use_deep else retriever.upper())
        model_display = model or os.getenv("SEANCE_MODEL") or os.getenv("MODEL") or "default-model"

        # --- retrieval feedback (shows in terminal before the spinner) --------
        if _env_bool("SEANCE_RETRIEVAL_LOG", True):
            unique_files = len({f for (_cid, f, _s, _e, _txt) in contexts})
            typer.secho(
                f"• Retrieval: {mode_label} | hits={len(matches)} | contexts={len(contexts)} | files={unique_files}",
                fg="blue",
            )

        # --- answer generation; tee stdout so verbose prints live AND is captured ---
        verbose_text = ""
        if active_verbose:
            # Stream to terminal immediately AND capture for transcript
            with _tee_stdout() as cap:
                answer, mode, reason = generate_answer(
                    question, contexts, use_llm=not no_llm, model=model, verbose=True
                )
            verbose_text = cap.getvalue() or ""
        else:
            with _spinner(f"{mode_label} | LLM (model={model_display}) is thinking…"):
                answer, mode, reason = generate_answer(
                    question, contexts, use_llm=not no_llm, model=model, verbose=False
                )

        typer.echo("")
        typer.secho("━━━━━━━━━━━━ RESPONSE ━━━━━━━━━━━━", fg="magenta")
        typer.echo(answer)
        typer.secho("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", fg="magenta")

        typer.secho(
            f"• Answer mode: {'LLM' if mode=='llm' else 'fallback'}"
            + (f" (model={model_display})" if mode=='llm' else (f" — {reason}" if reason else "")),
            fg=("green" if mode == "llm" else "yellow"),
        )

        meta_out = {"sources": sources_out} if session.info.show_sources else {}
        if active_verbose and verbose_text:
            meta_out["verbose_log"] = verbose_text

        session.append_message("assistant", answer, meta=meta_out)

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
        typer.echo("  :k <n>                  Set top-k retrieval")
        typer.echo("  :sources on|off         Toggle source printing")
        typer.echo("  :deep on|off            Toggle deep (whole-file) context")
        typer.echo("  :wide on|off            Toggle wide (tiny snippets from many files)")
        typer.echo("  :verbose on|off         Toggle verbose agent logs")
        typer.echo("  :env                    Reload .env now (no reindex)")
        typer.echo("  :show session           Print session folder paths")
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
    if parts[0] == ":wide":
        if len(parts) >= 2 and parts[1] in ("on", "off"):
            val = parts[1] == "on"
            session.meta = getattr(session, "meta", {})
            session.meta["wide"] = val
            typer.secho(f"• wide = {val}", fg="green")
        else:
            typer.secho("Usage: :wide on|off", fg="red")
        return
    if parts[0] == ":env":
        try:
            loaded = EnvUtils.load_env_for_tool()
            typer.secho(f"• env reloaded ({len(loaded)} sources)", fg="green")
        except Exception as e:
            typer.secho(f"• env reload failed: {e}", fg="red")
        return
    if parts[0] == ":verbose":
        if len(parts) >= 2 and parts[1] in ("on", "off"):
            val = parts[1] == "on"
            session.meta = getattr(session, "meta", {})
            session.meta["verbose"] = val
            typer.secho(f"• verbose = {val}", fg="green")
        else:
            typer.secho("Usage: :verbose on|off", fg="red")
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
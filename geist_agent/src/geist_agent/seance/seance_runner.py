# src/geist_agent/seance/seance_runner.py
from __future__ import annotations

from pathlib import Path
import os
import re
import typer

import sys
import threading
import time
from contextlib import contextmanager

from geist_agent.utils import ReportUtils, walk_files_compat as walk_files, EnvUtils

from .seance_index import (
    connect as seance_connect,
    build_index as seance_build_index,
    load_manifest, index_path, seance_dir
)
from .seance_query import retrieve, generate_answer
from .seance_session import SeanceSession
from .seance_common import tokenize

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

# ---------- rerank & doc-demotion helpers -------------------------------------
_DOC_SUFFIXES = {".md", ".rst", ".txt"}
_DOC_NAMES = {"readme", "license", "changelog", "contributing"}

def _is_doc(path: str) -> bool:
    p = path.lower()
    if any(seg in p for seg in ("/docs/", "/doc/", "/guides/", "/examples/")):
        return True
    base = p.rsplit("/", 1)[-1]
    stem = base.split(".", 1)[0]
    if stem in _DOC_NAMES:
        return True
    return any(p.endswith(s) for s in _DOC_SUFFIXES)

def _tokenize_simple(s: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9_]+", s.lower()))

def _postprocess_contexts(question: str,
                          contexts: list[tuple[str, str, int, int, str]],
                          target_ctx: int,
                          mode_label: str) -> tuple[list[tuple[str,str,int,int,str]], list[str]]:
    """
    Re-rank contexts by (1) passing a min on-topic floor, (2) query-hit count desc,
    (3) non-docs before docs; optionally cap docs by fraction; trim to target size.
    Also prints query terms and per-context hit counts when SEANCE_RETRIEVAL_LOG=true.
    """
    min_qhits      = _env_int("SEANCE_MIN_QHITS", 1)
    demote_docs    = _env_bool("SEANCE_DEMOTE_DOCS", True)
    doc_max_frac_s = os.getenv("SEANCE_DOC_MAX_FRAC", "0.3")
    try:
        doc_max_frac = float(doc_max_frac_s)
    except Exception:
        doc_max_frac = 0.3

    qterms = [t for t in _tokenize_simple(question) if len(t) > 1]
    qset   = set(qterms)

    enriched = []
    for cid, f, s, e, preview in contexts:
        toks = _tokenize_simple(preview)
        qhits = sum(1 for t in qset if t in toks)
        doc = _is_doc(f)
        # sort key: first group that passes min_qhits (0 best), then qhits desc,
        # then non-docs before docs, then shorter slice first, then file name tie-breaker
        key = (0 if qhits >= min_qhits else 1, -qhits, 1 if doc else 0, (e - s), f)
        enriched.append((key, cid, f, s, e, preview, qhits, doc))

    enriched.sort(key=lambda x: x[0])

    # Optional doc cap (fraction of final target)
    if demote_docs and target_ctx > 0:
        max_docs = max(0, int(doc_max_frac * target_ctx))
        kept, docs = [], 0
        for item in enriched:
            if len(kept) >= target_ctx:
                break
            is_doc = item[7]
            if is_doc and docs >= max_docs:
                continue
            kept.append(item)
            if is_doc:
                docs += 1
        enriched = kept
    else:
        enriched = enriched[:target_ctx]

    # Logging signal (query terms + per-context hits)
    if _env_bool("SEANCE_RETRIEVAL_LOG", True):
        qt = ", ".join(sorted(set(qterms))[:8]) or "∅"
        on_topic = sum(1 for it in enriched if it[6] >= min_qhits)
        typer.secho(f"• Query terms: {qt}  | qhits≥{min_qhits}: {on_topic}/{len(enriched)}", fg="blue")
        if _env_bool("SEANCE_LOG_CONTEXT_HITS", False):
            for it in enriched:
                _, _cid, f, s, e, _txt, qh, is_doc = it
                typer.secho(f"  - {f}:{s}-{e} | {'doc' if is_doc else 'code'} | qhits={qh}", fg="blue")

    final_contexts = [(it[1], it[2], it[3], it[4], it[5]) for it in enriched]
    sources_out    = [f"{it[2]}:{it[3]}-{it[4]}" for it in enriched]
    return final_contexts, sources_out


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
    deep: bool = typer.Option(False, "--deep", help="Search more within each top file"),
    wide: bool = typer.Option(False, "--wide", help="Cover many files with small slices"),
    env_reload: bool = typer.Option(False, "--env", help="Reload .env before answering"),

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
    typer.secho(
    "Type your questions. Commands: :help, :q, :k <n>, :sources on|off, :deep on|off, :wide on|off, :env, :verbose on|off, :show session",
    fg="cyan"
)
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
        q_deep  = "--deep"  in q_tokens
        q_wide  = "--wide"  in q_tokens
        q_env   = "--env"   in q_tokens
        q_verbose = "--verbose" in q_tokens

        # strip inline flags from the actual question
        for flag in ("--deep","--wide","--env","--verbose"):
            if flag in q_tokens:
                q_tokens = [t for t in q_tokens if t != flag]
        question = " ".join(q_tokens).strip()

        # If only toggles → set for next turn and continue
        if not question:
            session.meta = getattr(session, "meta", {})
            if q_deep:   session.meta["deep"] = True;  typer.secho("• deep = True", fg="green")
            if q_wide:   session.meta["wide"] = True;  typer.secho("• wide = True", fg="green")
            if q_env:
                loaded = []
                try:
                    loaded = EnvUtils.load_env_for_tool()
                except Exception:
                    pass
                typer.secho(f"• env reloaded ({len(loaded)} sources)", fg="green")
            if q_verbose: session.meta["verbose"] = True; typer.secho("• verbose = True", fg="green")
            continue

        session.append_message("user", question)
        typer.secho("⋯ retrieving context …", fg="blue")

        # Decide whether deep/wide/verbose/env are in effect this turn
        use_deep = deep
        use_wide = wide
        active_verbose = verbose

        if hasattr(session, "meta") and isinstance(session.meta, dict):
            use_deep = session.meta.get("deep", use_deep)
            use_wide = session.meta.get("wide", use_wide)
            active_verbose = session.meta.get("verbose", active_verbose)

        if q_deep:   use_deep = True
        if q_wide:   use_wide = True
        if q_verbose: active_verbose = True
        if env_reload or q_env:
            try:
                loaded = EnvUtils.load_env_for_tool()
                typer.secho(f"• env reloaded ({len(loaded)} sources)", fg="green")
            except Exception:
                typer.secho("• env reload failed (ignored)", fg="red")

        # If both deep & wide are enabled, prefer WIDE (explicit breadth)
        if use_deep and use_wide:
            typer.secho("• both --deep and --wide set → favoring --wide for this turn", fg="yellow")
            use_deep = False

        # Which retriever?
        retriever = (os.getenv("SEANCE_RETRIEVER") or "bm25").strip().lower()
        retriever = "bm25" if retriever not in ("bm25", "jaccard") else retriever

        # Candidate widening
        widen     = _env_int("SEANCE_WIDEN", 2)
        deep_mult = _env_int("SEANCE_DEEP_MULT", 4)
        wide_mult = _env_int("SEANCE_WIDE_MULT", 4)

        if use_wide:
            retrieve_k = session.info.k * wide_mult
        elif use_deep:
            retrieve_k = session.info.k * deep_mult
        else:
            retrieve_k = session.info.k * widen

        matches = retrieve(root, name, question, k=retrieve_k)


        man = load_manifest(root, name)  # refresh
        contexts, sources_out = [], []

        # --- If --wide or --deep flags are used ---
        if use_wide:
            # ── WIDE: many files, few slices each ───────────────────────────────
            max_contexts = _env_int("SEANCE_WIDE_MAX_CONTEXTS", session.info.k * 2)
            per_file_cap = _env_int("SEANCE_WIDE_PER_FILE", 1)
            per_file_cap2= _env_int("SEANCE_WIDE_PER_FILE_2", 2)  # 2nd pass allowance
            surround     = _env_int("SEANCE_WIDE_SURROUND", 30)
            ctx_char_cap = _env_int("SEANCE_WIDE_MAX_CHARS_PER_CTX", 1000)

            from collections import defaultdict
            taken_per_file: dict[str, int] = defaultdict(int)
            contexts = []

            # 1st pass: at most per_file_cap slices per file
            for cid, _score in matches:
                if len(contexts) >= max_contexts:
                    break
                meta = man.chunks.get(cid)
                if not meta:
                    continue
                if taken_per_file[meta.file] >= per_file_cap:
                    continue

                fp = root / meta.file
                try:
                    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                    start = max(1, meta.start_line - surround)
                    end   = min(len(lines), meta.end_line + surround)
                    preview = "\n".join(lines[start - 1:end])
                except Exception:
                    start, end, preview = meta.start_line, meta.end_line, "(unreadable chunk)"

                if len(preview) > ctx_char_cap:
                    preview = preview[:ctx_char_cap]

                contexts.append((cid, meta.file, start, end, preview))
                taken_per_file[meta.file] += 1

            # 2nd pass: allow up to per_file_cap2 if we're still short
            if len(contexts) < max_contexts and per_file_cap2 > per_file_cap:
                for cid, _score in matches:
                    if len(contexts) >= max_contexts:
                        break
                    meta = man.chunks.get(cid)
                    if not meta:
                        continue
                    if taken_per_file[meta.file] >= per_file_cap2:
                        continue

                    fp = root / meta.file
                    try:
                        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                        start = max(1, meta.start_line - surround)
                        end   = min(len(lines), meta.end_line + surround)
                        preview = "\n".join(lines[start - 1:end])
                    except Exception:
                        start, end, preview = meta.start_line, meta.end_line, "(unreadable chunk)"

                    if len(preview) > ctx_char_cap:
                        preview = preview[:ctx_char_cap]

                    contexts.append((cid, meta.file, start, end, preview))
                    taken_per_file[meta.file] += 1

            # Re-rank + doc-demote + trim
            contexts, sources_out = _postprocess_contexts(question, contexts, min(max_contexts, len(contexts)), "WIDE")


        elif use_deep:
            # ── DEEP: top files, several slices within each ─────────────────────
            top_files    = _env_int("SEANCE_DEEP_TOP_FILES", 3)
            max_contexts = _env_int("SEANCE_DEEP_MAX_CONTEXTS", session.info.k * 2)
            per_file_cap = _env_int("SEANCE_DEEP_PER_FILE", 4)
            surround     = _env_int("SEANCE_DEEP_SURROUND", 60)
            ctx_char_cap = _env_int("SEANCE_DEEP_MAX_CHARS_PER_CTX", 1600)

            file_scores: dict[str, float] = {}
            for cid, score in matches:
                meta = man.chunks.get(cid)
                if meta:
                    file_scores[meta.file] = file_scores.get(meta.file, 0.0) + float(score)
            ranked_files = [f for (f, _tot) in sorted(file_scores.items(), key=lambda kv: kv[1], reverse=True)[:top_files]]

            from collections import defaultdict
            taken_per_file: dict[str, int] = defaultdict(int)
            contexts = []

            for cid, _score in matches:
                if len(contexts) >= max_contexts:
                    break
                meta = man.chunks.get(cid)
                if not meta or meta.file not in ranked_files:
                    continue
                if taken_per_file[meta.file] >= per_file_cap:
                    continue

                fp = root / meta.file
                try:
                    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                    start = max(1, meta.start_line - surround)
                    end   = min(len(lines), meta.end_line + surround)
                    preview = "\n".join(lines[start - 1:end])
                except Exception:
                    start, end, preview = meta.start_line, meta.end_line, "(unreadable chunk)"

                if len(preview) > ctx_char_cap:
                    preview = preview[:ctx_char_cap]

                contexts.append((cid, meta.file, start, end, preview))
                taken_per_file[meta.file] += 1

            # Re-rank + doc-demote + trim
            contexts, sources_out = _postprocess_contexts(question, contexts, min(max_contexts, len(contexts)), "DEEP")


        else:
            diversify = _env_bool("SEANCE_DIVERSIFY_FILES", True)
            min_unique = _env_int("SEANCE_MIN_UNIQUE_FILES", max(1, min(5, session.info.k)))

            seen_files = set()
            contexts = []

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
                if len(contexts) >= session.info.k and len(seen_files) >= min_unique:
                    break

            # If we didn't reach min_unique, sweep again for new files:
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

            # Re-rank + doc-demote + trim
            contexts, sources_out = _postprocess_contexts(question, contexts, min(session.info.k, len(contexts)), retriever.upper())



        # Spinner shows which retrieval path ran
        if use_wide:
            mode_label = "WIDE"
        elif use_deep:
            mode_label = "DEEP"
        else:
            mode_label = retriever.upper()

        model_display = model or os.getenv("SEANCE_MODEL") or os.getenv("MODEL") or "default-model"

        # --- retrieval feedback (shows in terminal before the spinner) --------
        if _env_bool("SEANCE_RETRIEVAL_LOG", True):
            unique_files = len({f for (_cid, f, _s, _e, _txt) in contexts})
            typer.secho(
                f"• Retrieval: {mode_label} | hits={len(matches)} | contexts={len(contexts)} | files={unique_files}",
                fg="blue",
            )

        if active_verbose:
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

        typer.secho(
            f"• Answer mode: {'LLM' if mode=='llm' else 'fallback'}"
            + (f" (model={model_display})" if mode=='llm' else (f" — {reason}" if reason else "")),
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
        typer.echo("  :k <n>                  Set top-k retrieval")
        typer.echo("  :sources on|off         Toggle source printing")
        typer.echo("  :deep on|off            Toggle deep mode (more within top files)")
        typer.echo("  :wide on|off            Toggle wide mode (a little from many files)")
        typer.echo("  :env                    Reload .env now (no reindex)")
        typer.echo("  :verbose on|off         Toggle verbose agent logs")
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
            from geist_agent.utils import EnvUtils  # already imported at top, but re-import safe
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
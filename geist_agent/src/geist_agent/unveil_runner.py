# src/geist_agent/unveil_runner.py
from __future__ import annotations
from pathlib import Path
from typing import List, Optional, Dict
import json
import sys
import time
from contextlib import contextmanager
from geist_agent.utils import DEFAULT_EXTS, SKIP_DIRS, walk_files_compat as walk_files

from crewai import Task
# NOTE: we intentionally do NOT import UnveilCrew here to avoid any accidental CrewBase bootstrapping.
from geist_agent.unveil_tools import (
    chunk_file, static_imports,
    infer_edges_and_externals, components_from_paths, render_report
)

import os, logging
# Silence CrewAI and friends
os.environ.setdefault("CREWAI_LOG_LEVEL", "ERROR")
for name in ("crewai", "langchain", "httpx", "urllib3"):
    logging.getLogger(name).setLevel(logging.ERROR)

# --- per-tool LLM env overlays (UNVEIL_*) ---
_LLM_KEYS = [
    "MODEL", "API_BASE",
    "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
    "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
    "XAI_API_KEY", "COHERE_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY",
]

def _apply_prefixed_env(prefix: str):
    for key in _LLM_KEYS:
        val = os.getenv(f"{prefix}_{key}")
        if val:
            os.environ[key] = val

@contextmanager
def _llm_profile(prefix: str):
    saved = {k: os.environ.get(k) for k in _LLM_KEYS}
    try:
        _apply_prefixed_env(prefix)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

# ---------- agents: load configs (YAML with safe fallbacks) ----------
def _get_unveil_agents():
    from pathlib import Path
    import yaml
    from crewai import Agent

    cfg = Path(__file__).resolve().parent / "config" / "unveil_agents.yaml"
    data = {}
    if cfg.is_file():
        with cfg.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    def _mk(name, fallback):
        if name in data:
            # Force quiet + small iterations even if YAML says otherwise
            return Agent(
                config=data[name],
                verbose=False,
                max_iter=2,              # keep tiny
                cache=True,
                max_execution_time=120,  # seconds hard cap
                respect_context_window=True,
            )
        return fallback

    file_analyst = _mk(
        "unveil_file_analyst",
        Agent(
            role="Code File Analyst",
            goal=("Read a file chunk-wise and produce JSON: "
                  "{role, api[], summary[], suspects_deps[], callers_guess[]}"),
            backstory="Fast, pragmatic code reader focused on useful outputs.",
            verbose=False,
            max_iter=2,
            cache=True,
            max_execution_time=90,
            respect_context_window=True,
        ),
    )
    architect = _mk(
        "unveil_architect",
        Agent(
            role="System Architect",
            goal=("Write a concise repo overview (entry points, main flows, "
                  "collaboration patterns, notable components)."),
            backstory="Communicates architecture clearly for new engineers.",
            verbose=False,
            max_iter=1,               # 1 pass is plenty for the overview
            cache=True,
            max_execution_time=60,
            respect_context_window=True,
        ),
    )
    return file_analyst, architect


# NOT BEING CALLED DELETE & VERIFY NO ISSUES
# ---------- tiny CLI utils (stdout logger, JSON fence parser) ----------
def _log(enabled: bool, msg: str):
    if enabled:
        print(msg, file=sys.stdout, flush=True)


def _parse_json_maybe_fenced(s: str) -> dict:
    """Accept raw model output; strip ```json fences if present, return dict or empty structure."""
    txt = str(s).strip()
    if txt.startswith("```"):
        # carve out ```json ... ```
        if txt.lower().startswith("```json"):
            txt = txt[7:]
        else:
            txt = txt[3:]
        if "```" in txt:
            txt = txt.split("```", 1)[0]
    try:
        return json.loads(txt)
    except Exception:
        return {"role": "", "api": [], "summary": [], "suspects_deps": [], "callers_guess": []}


# ---------- command entry ----------
def run_unveil(
    path: str,
    include: Optional[List[str]],
    exclude: List[str],
    exts: Optional[List[str]],
    max_files: int,
    title: str = "Unveil: Codebase Map",
    verbose: bool = True,
) -> Path:
    # --- tiny local logger so type checkers are happy
    def _log(show: bool, msg: str) -> None:
        if show:
            print(msg, file=sys.stderr, flush=True)

    def _parse_json_maybe_fenced(s: str) -> dict:
        """Accept plain JSON or ```json fenced blocks; return {} on failure."""
        txt = str(s).strip()
        if "```" in txt:
            # Extract first fenced block if present
            start = txt.find("```")
            end = txt.find("```", start + 3)
            if end > start:
                block = txt[start + 3:end]
                # strip possible language tag like "json\n"
                first_nl = block.find("\n")
                if first_nl != -1:
                    block = block[first_nl + 1 :]
                txt = block.strip()
        try:
            return json.loads(txt)
        except Exception:
            return {}

    include = include or []
    exts = [e.lower() for e in (exts or [])] or None

    root = Path(path).resolve()
    _log(verbose, f"▶ Scanning: {root}")
    files = walk_files(root, include, exclude, exts, max_files)
    _log(verbose, f"• Files found: {len(files)}")

    # --- 1) Chunk + static imports
    chunks_map: Dict[str, List[str]] = {}
    static_map: Dict[str, List[str]] = {}
    for i, f in enumerate(files, 1):
        rel = f.relative_to(root).as_posix()
        chunks_map[rel] = chunk_file(f)
        static_map[rel] = static_imports(f)
        if verbose and i % max(1, len(files) // 10) == 0:
            _log(verbose, f"• Preprocessed {i}/{len(files)} files")

    # --- 2) File-level summaries via File Analyst (LLM)
    start = time.time()
    # Load .env if your utils provide it (safe if missing)
    try:
        from geist_agent.utils import EnvUtils
        if hasattr(EnvUtils, "load_env_for_tool"):
            EnvUtils.load_env_for_tool()
    except Exception:
        pass

    # Apply UNVEIL_* overrides just for agent creation/execution
    with _llm_profile("UNVEIL"):
        file_analyst, architect = _get_unveil_agents()

    _log(verbose, "• Summarizing files with File Analyst…")
    summaries: Dict[str, dict] = {}

    total = len(chunks_map)
    for i, (rel, chunks) in enumerate(chunks_map.items(), 1):
        t0 = time.time()
        _log(verbose, f"  → Summarizing {rel} ({i}/{total})…")

        prompt = (
            "You are analyzing a single code file. "
            "Return *pure JSON* with keys exactly:\n"
            "  role: short purpose of the file,\n"
            "  api: array of public functions/classes it exposes,\n"
            "  summary: 3–6 bullet points explaining what it does and how it interacts,\n"
            "  suspects_deps: array of internal files/modules it likely depends on (names only),\n"
            "  callers_guess: array of modules/files likely to call this.\n\n"
            f"File: {rel}\n"
            "Context (first 2 chunks):\n"
            + "\n---\n".join(chunks[:2])
        )

        t = Task(description=prompt, expected_output="Return only valid JSON.")
        try:
            ans = file_analyst.execute_task(t)
            data = _parse_json_maybe_fenced(ans)
            if not isinstance(data, dict):
                data = {}
        except Exception as e:
            _log(verbose, f"    ⚠️ Failed summarizing {rel}: {type(e).__name__}")
            data = {}

        # Fill defaults defensively
        data = {
            "role": data.get("role", ""),
            "api": data.get("api", []) or [],
            "summary": data.get("summary", []) or [],
            "suspects_deps": data.get("suspects_deps", []) or [],
            "callers_guess": data.get("callers_guess", []) or [],
        }

        summaries[rel] = data
        dt = time.time() - t0
        _log(verbose, f"  ← Done {rel} in {dt:0.1f}s ({i}/{total})")


    # --- 3) Static-linking (deterministic) + externals
    _log(verbose, "• Inferring edges/components…")
    edges, externals = infer_edges_and_externals(root, files, static_map)
    components = components_from_paths([p.relative_to(root).as_posix() for p in files])

    # --- 4) Repo narrative via Architect (LLM)
    _log(verbose, "• Writing repo overview with Architect…")
    compact_roles = "\n".join(f"- {k}: {v.get('role','')}" for k, v in list(summaries.items())[:20])
    prompt_repo = (
        "Write a concise, engineer-friendly overview (8–12 sentences) of this repository:\n"
        f"Title: {title}\n"
        "Include: entry points, main flows, how parts collaborate, key components, and notable patterns.\n"
        "Use short paragraphs or bullets. Avoid speculation.\n\n"
        "Some file roles:\n" + compact_roles + "\n\n"
        f"Edge count: {len(edges)}, Component count: {len(components)}"
    )
    arch_task = Task(description=prompt_repo, expected_output="A short Markdown overview.")
    try:
        raw = architect.execute_task(arch_task)
        narrative = str(raw).strip()
        MAX_CHARS = 5000
        if len(narrative) > MAX_CHARS:
            narrative = narrative[:MAX_CHARS] + "\n\n*(truncated)*"
    except Exception as e:
        narrative = f"Overview not available (architect step failed: {type(e).__name__})."
    summaries["__repo__"] = {"narrative": narrative}

    # --- 5) Render
    _log(verbose, "• Rendering report…")

    # derive safe base name from the repo root for filename
    repo_name = root.name or "unknown_root"

    out_path = render_report(
        title=title,
        root=root,
        file_summaries=summaries,
        edges=edges,
        components=components,
        externals=externals,
        reports_subfolder="unveil_reports",
        filename_topic=root.name,   # 👈 pass Job_Hunter, geist_agent, etc
    )

    _log(verbose, f"✓ Done in {time.time()-start:.1f}s → {out_path}")
    return out_path


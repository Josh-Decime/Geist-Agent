# src/geist_agent/unveil_runner.py
from __future__ import annotations
from pathlib import Path
from typing import List, Optional, Dict
import json
import sys
import time

from crewai import Task
# NOTE: we intentionally do NOT import UnveilCrew here to avoid any accidental CrewBase bootstrapping.
from geist_agent.unveil_tools import (
    walk_files, chunk_file, static_imports,
    infer_edges_and_externals, components_from_paths, render_report
)


# --- helper to obtain agents from unveil_agents.yaml, with safe inline fallback
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
            # keep the model lightweight; cap iterations to reduce Ollama load
            return Agent(config=data[name], verbose=False, max_iter=3, cache=True)
        return fallback

    file_analyst = _mk(
        "unveil_file_analyst",
        Agent(
            role="Code File Analyst",
            goal=("Read a file chunk-wise and produce JSON: "
                  "{role, api[], summary[], suspects_deps[], callers_guess[]}"),
            backstory="Fast, pragmatic code reader focused on useful outputs.",
            verbose=False,
            max_iter=3,
            cache=True,
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
            max_iter=3,
            cache=True,
        ),
    )
    return file_analyst, architect


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


def run_unveil(
    path: str,
    include: Optional[List[str]],
    exclude: List[str],
    exts: Optional[List[str]],
    max_files: int,
    title: str = "Unveil: Codebase Map",
    verbose: bool = True,
) -> Path:
    _log(verbose, f"🔎 Scanning: {Path(path).resolve()}")
    include = include or []
    exts = [e.lower() for e in (exts or [])] or None

    root = Path(path).resolve()
    files = walk_files(root, include, exclude, exts, max_files)
    _log(verbose, f"📄 Files found: {len(files)}")

    # --- 1) Chunk + static imports
    chunks_map: Dict[str, List[str]] = {}
    static_map: Dict[str, List[str]] = {}
    t0 = time.time()
    for i, f in enumerate(files, 1):
        rel = f.relative_to(root).as_posix()
        chunks_map[rel] = chunk_file(f)
        static_map[rel] = static_imports(f)
        if verbose and (i % 25 == 0 or i == len(files)):
            _log(verbose, f"✂️  Preprocessed {i}/{len(files)} files")

    # --- 2) File-level summaries via File Analyst (LLM)
    file_analyst, architect = _get_unveil_agents()
    _log(verbose, "🧠 Summarizing files with File Analyst…")
    summaries: Dict[str, dict] = {}
    for i, (rel, chunks) in enumerate(chunks_map.items(), 1):
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
            data = _parse_json_maybe_fenced(str(ans))
        except Exception:
            data = {"role": "", "api": [], "summary": [], "suspects_deps": [], "callers_guess": []}
        summaries[rel] = data
        if verbose and (i % 10 == 0 or i == len(chunks_map)):
            _log(verbose, f"📝 Summarized {i}/{len(chunks_map)} files")

    # --- 3) Static-linking (deterministic) + externals
    _log(verbose, "🧷 Inferring edges/components…")
    edges, externals = infer_edges_and_externals(root, files, static_map)
    components = components_from_paths([p.relative_to(root).as_posix() for p in files])

    # --- 4) Repo narrative via Architect (LLM)
    _log(verbose, "🏗️ Writing repo overview with Architect…")
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
        narrative = str(architect.execute_task(arch_task)).strip()
    except Exception:
        narrative = "Overview not available (architect step failed)."
    summaries["__repo__"] = {"narrative": narrative}

    # --- 5) Render
    _log(verbose, "🖨️  Rendering report…")
    out_path = render_report(
        title=title,
        root=root,
        file_summaries=summaries,
        edges=edges,
        components=components,
        externals=externals,
    )
    _log(verbose, f"✅ Done in {time.time() - t0:.1f}s")
    return out_path

# src/geist_agent/poltern.py
import typer
from datetime import datetime

from typing import List, Optional

from geist_agent.utils import EnvUtils
EnvUtils.load_env_for_tool()

from geist_agent.scrying import ScryingAgent
from geist_agent import doctor as doctor_mod

from geist_agent.unveil_agent import UnveilCrew, UnveilInputs
from pathlib import Path
import json

app = typer.Typer(help="Poltergeist CLI")

# ---------- scry --------------
@app.command(
    "scry",
    help="Research a topic and write a report.",
    epilog=(
        "Examples:\n"
        "  poltergeist scry --topic \"Custom Topic\"\n"
        "  poltergeist scry -t \"Custom Topic\"\n"
    ),
)
def scry(
    topic: str = typer.Option("The Meaning of Life", "--topic", "-t", help="What to scry about")
):
    inputs = {"topic": topic, "current_year": str(datetime.now().year)}
    s = ScryingAgent()
    s.set_topic(topic)
    s.scrying().kickoff(inputs=inputs)

# ----------- doctor ----------
@app.command("doctor", help="Diagnostics (version, env, Ollama, report write).")
def doctor(
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of rich table"),
):
    code = doctor_mod.run(as_json=as_json)
    raise typer.Exit(code)

# ---------- unveil ----------
@app.command(
    "unveil",
    help="Agentic codebase scan: summaries, components, dependency graph, Markdown output.",
    epilog=(
        "Examples:\n"
        "  poltergeist unveil --path .\n"
        "  poltergeist unveil -p .. --exclude .venv node_modules dist build --max-files 800\n"
        "  poltergeist unveil -p . --ext .py --ext .ts\n"
    ),
)
def unveil_cmd(
    path: str = typer.Option(".", "--path", "-p", help="Root folder to scan"),
    include: Optional[List[str]] = typer.Option(None, "--include", help="Optional include prefixes (repeatable)", show_default=False),
    exclude: List[str] = typer.Option(
        [".venv","venv","node_modules",".git","dist","build","__pycache__"],
        "--exclude", help="Exclude path prefixes (repeatable)"
    ),
    ext: Optional[List[str]] = typer.Option(None, "--ext", help="Limit to extensions (repeatable) e.g. --ext .py --ext .ts", show_default=False),
    max_files: int = typer.Option(800, "--max-files", help="Limit number of files scanned"),
):
    """
    QUICK TEST PATH:
    - Precompute file list + static imports
    - (No LLM yet) Generate a Markdown report so we can verify end-to-end IO.
    """
    from pathlib import Path
    from collections import Counter
    from geist_agent.unveil_tools import walk_files, chunk_file, static_imports, render_report

    include = include or []
    exts = [e.lower() for e in (ext or [])] or None

    root = Path(path).resolve()
    if not root.exists():
        typer.secho(f"Path not found: {root}", fg="red")
        raise typer.Exit(2)

    # 1) Walk files
    files = walk_files(root, include=include, exclude=exclude, exts=exts, max_files=max_files)
    if not files:
        typer.secho("No files matched filters.", fg="yellow")
        raise typer.Exit(0)

    # 2) Chunk + static imports (for now we don’t call an LLM)
    file_summaries = {}
    externals_counter = Counter()
    for p in files:
        rel = p.relative_to(root).as_posix()
        # Chunking is ready for later LLM passes (not used by render_report yet)
        _chunks = chunk_file(p, max_chars=4000)
        deps = static_imports(p)
        # Heuristic: count external deps that aren’t obvious relative paths
        for d in deps:
            if not d.startswith((".", "/")):
                externals_counter[d] += 1
        file_summaries[rel] = {
            "role": "",             # LLM will fill later
            "api": [],              # LLM will fill later
            "suspects_deps": deps,  # seed signals
            "callers_guess": [],    # LLM will fill later
        }

    # 3) (Optional) empty graph/components for now — LLM will infer later
    edges = []
    components = {}
    externals = dict(externals_counter)

    # 4) Seed an empty repo narrative (LLM will fill later)
    file_summaries["__repo__"] = {"narrative": ""}

    # 5) Render Markdown so you can confirm the pipeline is wired
    out_path = render_report(
        title="Unveil: Codebase Map",
        root=root,
        file_summaries=file_summaries,
        edges=edges,
        components=components,
        externals=externals,
    )
    typer.secho(f"🗺  Unveil report written to: {out_path}", fg="green")


# ---------- entry ----------
def main():
    app()

if __name__ == "__main__":
    main()

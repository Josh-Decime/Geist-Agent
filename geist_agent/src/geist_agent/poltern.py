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
    max_files: int = typer.Option(200, "--max-files", help="Limit number of files scanned"),
    use_llm: bool = typer.Option(True, "--llm/--no-llm", help="Generate per-file summaries with your local model"),
):
    """
    Hybrid quick path:
    - Walk files + static deps (precompute)
    - (Optional) call local LLM per file for JSON summary {role, api[]}
    - Render Markdown report
    """
    import os, json
    from pathlib import Path
    from collections import Counter
    from geist_agent.unveil_tools import walk_files, chunk_file, static_imports, render_report

    include = include or []
    exts = [e.lower() for e in (ext or [])] or None

    root = Path(path).resolve()
    if not root.exists():
        typer.secho(f"Path not found: {root}", fg="red")
        raise typer.Exit(2)

    files = walk_files(root, include=include, exclude=exclude, exts=exts, max_files=max_files)
    if not files:
        typer.secho("No files matched filters.", fg="yellow")
        raise typer.Exit(0)

    # Optional LLM helper
    def llm_file_summary(sample_text: str, rel_path: str) -> dict:
        """
        Ask the local model for a concise JSON summary for one file.
        Falls back quietly if anything goes wrong.
        """
        try:
            import litellm
            model = os.getenv("MODEL", "ollama/qwen2.5:7b-instruct")
            api_base = os.getenv("API_BASE", "http://localhost:11434")

            prompt = (
                "You are a code analyst. Given a single file's content, return a compact JSON with keys:\n"
                "role: a one-sentence purpose description;\n"
                "api: array of public-facing functions/classes/methods (names only);\n"
                "ONLY output JSON. No backticks, no commentary.\n\n"
                f"File: {rel_path}\n"
                f"Content sample:\n{sample_text[:2000]}\n"
            )
            resp = litellm.completion(
                model=model,
                api_base=api_base,  # used by ollama provider
                messages=[{"role": "user", "content": prompt}],
                timeout=60,
            )
            text = resp.choices[0].message.content if hasattr(resp.choices[0].message, "content") else str(resp)
            # Be forgiving: extract JSON if wrapped
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                text = text[start:end+1]
            data = json.loads(text)
            # ensure shape
            out = {
                "role": data.get("role") or "",
                "api": data.get("api") or [],
            }
            if not isinstance(out["api"], list):
                out["api"] = []
            return out
        except Exception:
            return {"role": "", "api": []}

    from collections import defaultdict
    file_summaries = {}
    externals_counter = Counter()

    for p in files:
        rel = p.relative_to(root).as_posix()
        chunks = chunk_file(p, max_chars=4000)
        deps = static_imports(p)
        for d in deps:
            if not d.startswith((".", "/")):
                externals_counter[d] += 1

        role, api = "", []
        if use_llm and chunks:
            llm = llm_file_summary(chunks[0], rel)
            role, api = llm.get("role", ""), llm.get("api", [])

        file_summaries[rel] = {
            "role": role,
            "api": api,
            "suspects_deps": deps,
            "callers_guess": [],
        }

    # Simple empty graph/components for now (linking comes next)
    edges = []
    components = {}
    externals = dict(externals_counter)

    # Seed an overview if you want (kept empty until we add the Architect pass)
    file_summaries["__repo__"] = {"narrative": ""}

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

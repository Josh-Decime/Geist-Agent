# src/geist_agent/poltern.py
import typer
from datetime import datetime
from typing import List

from geist_agent.utils import EnvUtils
loaded_sources = EnvUtils.load_env_for_tool()
print(f"• Loaded .env sources: {loaded_sources}")

from geist_agent.scrying import ScryingAgent
from geist_agent import doctor as doctor_mod
from geist_agent.unveil.unveil_runner import run_unveil
from geist_agent.ward.ward_runner import run_ward as ward_run
from geist_agent.seance import seance_runner as seance_mod

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
    path: str = typer.Option(".", "--path", "-p"),
    include: List[str] = typer.Option(None, "--include", show_default=False),
    exclude: List[str] = typer.Option(
        [".venv","venv","node_modules",".git","dist","build","__pycache__"], "--exclude"
    ),
    ext: List[str] = typer.Option(None, "--ext", show_default=False),
    max_files: int = typer.Option(800, "--max-files"),
    full: bool = typer.Option(False, "--full", help="Use broad file profile (configs/docs/assets)."),
):
    out = run_unveil(
        path=path,
        include=include,
        exclude=exclude,
        exts=ext,
        max_files=max_files,
        title="Unveil: Codebase Map",
        verbose=True,
        full=full,            # <-- pass through
    )
    typer.secho(f"Unveil report written to:  {out}", fg="green")

# ---------- ward --------------
@app.command("ward", help="Run security audit (OSV + secrets + risky patterns + LLM recommendations)")
def ward_cmd(
    path: str = typer.Option(".", "--path", "-p", help="Project root to audit"),
    include: List[str] = typer.Option(None, "--include", help="Prefix filters (repeatable)"),
    exclude: List[str] = typer.Option(None, "--exclude", help="Prefix filters (repeatable)"),
    ext: List[str] = typer.Option(None, "--ext", help="Allowed extensions (repeatable)"),
    max_files: int = typer.Option(3000, "--max-files", help="Max files to scan"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress logs"),
    no_osv: bool = typer.Option(False, "--no-osv", help="Disable OSV if present"),
    no_redact: bool = typer.Option(False, "--no-redact", help="Do NOT redact secrets (discouraged)"),
    preview: bool = typer.Option(False, "--preview", help="Masked preview for secrets"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Disable LLM recommendations"),
    json: bool = typer.Option(False, "--json", help="Also write a JSON artifact for CI/diffs"),
):
    out = ward_run(
        path=path,
        include=include or None,
        exclude=exclude or None,
        exts=ext or None,
        max_files=max_files,
        verbose=not quiet,
        use_osv=not no_osv,
        redact=not no_redact,
        preview=preview,
        llm=not no_llm,   # ON by default
        write_json=json,  # OFF by default; enable with --json
    )
    typer.secho(f"Ward report written to: {out}", fg="green")

# ---------- seance ------------
@app.command(
    "seance",
    help="Connect to a filebase and ask questions about the codebase (or any supported text files).",
    epilog=(
        "Modes:\n"
        "  connect   Initialize .geist/seance/<name>/ (no indexing yet)\n"
        "  index     Build or update the index incrementally\n"
        "  chat      Interactive REPL; saves a transcript\n\n"
        "Examples:\n"
        "  poltergeist seance connect --path . --name app-core\n"
        "  poltergeist seance index --name app-core\n"
        "  poltergeist seance chat --name app-core\n"
    ),
)
def seance_cmd(
    mode: str = typer.Argument("chat", metavar="MODE", help="connect | index | chat (default: chat)"),
    # shared
    path: str = typer.Option(".", "--path", "-p", help="Root path of the filebase"),
    name: str = typer.Option(None, "--name", "-n", help="Seance name (default: derived from folder)"),
    # retrieval/answering knobs
    k: int = typer.Option(6, "--k", help="How many chunks to retrieve (ask/chat)"),
    show_sources: bool = typer.Option(True, "--show-sources/--no-show-sources", help="Show citations in output"),
    # index knobs
    max_chars: int = typer.Option(1200, "--max-chars", help="Max chars per chunk (index)"),
    overlap: int = typer.Option(150, "--overlap", help="Chunk overlap in chars (index)"),
    # chatting
    no_llm: bool = typer.Option(False, "--no-llm", help="Disable LLM; use extractive preview"),
    model: str = typer.Option(None, "--model", help="LLM model id (defaults from env)"),
    verbose: bool = typer.Option(False, "--verbose"),
    deep: bool = typer.Option(False, "--deep", help="Feed whole files (top hits) to the LLM"),
):
    """
    Wrapper so 'seance' behaves like our other single-entry commands.
    Routes to connect/index/ask/chat in geist_agent.seance.seance_runner.
    """
    mode = mode.strip().lower()

    if mode == "connect":
        # Initialize .geist/seance/<name> (no indexing)
        seance_mod.connect(path=path, name=name)
        return

    if mode == "index":
        # Build or update index
        seance_mod.index(path=path, name=name, max_chars=max_chars, overlap=overlap)
        return

    if mode == "chat":
        seance_mod.chat(
            path=path,
            name=name,
            k=k,
            show_sources=show_sources,
            no_llm=no_llm,
            model=model,
            verbose=verbose,
            deep=deep,
        )
        return

    raise typer.BadParameter("MODE must be one of: connect, index, ask, chat")

# ---------- entry ----------
def main():
    app()

if __name__ == "__main__":
    main()


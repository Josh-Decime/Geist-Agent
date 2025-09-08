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



# ---------- entry ----------
def main():
    app()

if __name__ == "__main__":
    main()


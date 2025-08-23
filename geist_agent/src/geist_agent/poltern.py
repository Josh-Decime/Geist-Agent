# src/geist_agent/poltern.py
import typer
from datetime import datetime
from typing import List

from geist_agent.utils import EnvUtils
EnvUtils.load_env_for_tool()

from geist_agent.scrying import ScryingAgent
from geist_agent import doctor as doctor_mod
from geist_agent.unveil_runner import run_unveil

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
    verbose: bool = typer.Option(True, "--verbose/--no-verbose", help="Show progress"),
):
    out = run_unveil(path, include, exclude, ext, max_files, title="Unveil: Codebase Map", verbose=verbose)
    typer.secho(f"🗺  Unveil report written to:  {out}", fg="green")



# ---------- entry ----------
def main():
    app()

if __name__ == "__main__":
    main()

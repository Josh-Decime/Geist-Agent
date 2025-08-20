# src/geist_agent/poltern.py
import typer
from datetime import datetime

from geist_agent.utils import EnvUtils
EnvUtils.load_env_for_tool()

from geist_agent.scrying import ScryingAgent
from geist_agent import doctor as doctor_mod

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

# ---------- entry ----------
def main():
    app()

if __name__ == "__main__":
    main()

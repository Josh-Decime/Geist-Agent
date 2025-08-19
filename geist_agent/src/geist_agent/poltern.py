# src/geist_agent/poltern.py
import typer
from datetime import datetime
from geist_agent.scrying import ScryingAgent
from geist_agent.utils import EnvUtils   

# Load env before doing anything else
EnvUtils.load_env_for_tool()

app = typer.Typer(help="Poltergeist CLI v0.1.2")

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

@app.command("doctor", help="Check Ollama connectivity and model availability.")
def doctor():
    import os, json, urllib.request
    base = os.getenv("API_BASE") or "http://localhost:11434"
    want = (os.getenv("MODEL") or "").split("/", 1)[-1]
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        names = [m["name"] for m in data.get("models", [])]
        typer.echo(f"Ollama reachable at {base}")
        typer.echo(f"Installed models: {names}")
        if want and not any(n.startswith(want) for n in names):
            raise RuntimeError(f"Model {want!r} not found. Try: ollama pull {want}")
        typer.secho("✅ Ready", fg="green")
    except Exception as e:
        typer.secho(f"❌ Not ready: {e}", fg="red")
        raise typer.Exit(1)

def main():
    app()

if __name__ == "__main__":
    main()

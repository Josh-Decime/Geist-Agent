# src/geist_agent/poltern.py
import typer
from datetime import datetime
from geist_agent.scrying import ScryingAgent

app = typer.Typer(help="Poltergeist CLI v0.1.2")

@app.command("scry", help="Direct Geist to research a topic")
def scry(
    topic: str = typer.Option("The Meaning of Life", "--topic", "-t", help="What to scry about")
):
    inputs = {"topic": topic, "current_year": str(datetime.now().year)}
    s = ScryingAgent()
    s.set_topic(topic)
    s.scrying().kickoff(inputs=inputs)

@app.command("version", help="Show version and confirm CLI wiring")
def version_cmd():
    typer.echo("poltergeist OK")

def main():
    app()

if __name__ == "__main__":
    main()

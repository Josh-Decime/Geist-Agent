# src/geist_agent/poltern.py
import typer
from datetime import datetime

from geist_agent.utils import EnvUtils
EnvUtils.load_env_for_tool()

from geist_agent.scrying import ScryingAgent
from geist_agent import doctor as doctor_mod

app = typer.Typer(help="Poltergeist CLI")

# ---------- scry ----------
@app.command(
    "scry",
    help="Run the Scrying workflow (or train/replay/test it).",
    epilog=(
        "Examples:\n"
        "  poltergeist scry --topic \"Custom Topic\"\n"
        "  poltergeist scry --train -t \"Astral parasites\"\n"
        "  poltergeist scry --test -t \"Poltergeists\"\n"
        "  poltergeist scry --replay --task-id 705efe35-1a9a-40a5-b650-29fc9e034666\n"
    ),
)
def scry(
    # common
    topic: str = typer.Option("The Meaning of Life", "--topic", "-t", help="What to scry about"),
    # modes (mutually exclusive)
    train: bool = typer.Option(False, "--train", help="Train (self-improve prompts)."),
    replay: bool = typer.Option(False, "--replay", help="ADVANCED: Replay from a specific task."),
    test: bool   = typer.Option(False, "--test",   help="Run multiple times and grade with an eval model."),
    # shared/defaulted params
    n_iterations: int = typer.Option(None, "--iterations", "-n", help="Iterations for --train/--test (default varies)."),
    filename: str = typer.Option(None, "--filename", help="Output notes file for --train (auto if omitted)."),
    eval_llm: str = typer.Option(None, "--eval-llm", help="Evaluation model for --test (defaults to EVAL_MODEL or MODEL)."),
    # replay param
    task_id: str = typer.Option(None, "--task-id", help="Task ID for --replay (from previous run)."),
):
    """
    Default: run once (kickoff). Supply exactly one of --train / --replay / --test to switch mode.
    """
    from os import getenv
    from geist_agent.utils import ReportUtils, PathUtils

    # --- validate exclusive mode ---
    modes_selected = sum(bool(x) for x in (train, replay, test))
    if modes_selected > 1:
        typer.secho("Error: Use only one of --train / --replay / --test.", fg="red")
        raise typer.Exit(2)

    # --- build workflow & inputs (from scrying.py) ---
    inputs = {"topic": topic, "current_year": str(datetime.now().year)}
    agent = ScryingAgent()
    agent.set_topic(topic)
    scry_workflow = agent.scrying()

    # --- TRAIN ---
    if train:
        # defaults
        iters = n_iterations if n_iterations is not None else 3
        # auto filename if not provided
        if not filename:
            # place training notes in a sibling folder to reports/scrying_reports
            train_dir = PathUtils.ensure_reports_dir("scrying_training")
            auto_name = "training_" + ReportUtils.generate_filename(topic)
            filename = str(train_dir / auto_name)

        scry_workflow.train(n_iterations=int(iters), filename=filename, inputs=inputs)
        typer.echo(f"Training complete. Notes: {filename}")
        return

    # --- REPLAY (advanced) ---
    if replay:
        if not task_id:
            typer.secho("Error: --replay requires --task-id (from a prior run).", fg="red")
            raise typer.Exit(2)
        scry_workflow.replay(task_id=task_id)
        return

    # --- TEST ---
    if test:
        iters = n_iterations if n_iterations is not None else 2
        # default eval_llm: EVAL_MODEL env then MODEL
        model = eval_llm or getenv("EVAL_MODEL") or getenv("MODEL")
        if not model:
            typer.secho("Error: --test needs --eval-llm, or set EVAL_MODEL/MODEL in your .env.", fg="red")
            raise typer.Exit(2)
        scry_workflow.test(n_iterations=int(iters), eval_llm=model, inputs=inputs)
        return

    # --- default: RUN ONCE ---
    scry_workflow.kickoff(inputs=inputs)


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

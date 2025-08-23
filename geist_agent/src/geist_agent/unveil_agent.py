# src/geist_agent/unveil_agent.py
from __future__ import annotations
from pathlib import Path
from typing import List
import yaml

from crewai import Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task
from crewai.agents.agent_builder.base_agent import BaseAgent

@CrewBase
class UnveilCrew:
    """Unveil crew (CrewAI) that isolates to unveil_* config and ignores unrelated tasks."""

    agents: List[BaseAgent]
    tasks: List[Task]

    # Only load unveil_* configs; avoid pulling in scry tasks/agents.
    def load_configurations(self):
        here = Path(__file__).resolve().parent
        cfg = here / "config"

        def _load(p: Path):
            if p.exists():
                with p.open("r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            return {}

        agents_raw = _load(cfg / "unveil_agents.yaml") or _load(cfg / "agents.yaml")
        agents_raw = {k: v for k, v in (agents_raw or {}).items() if k.startswith("unveil_")}
        self.agents_config = agents_raw

        # The runner creates Task() objects manually, so tasks are optional.
        tasks_raw = _load(cfg / "unveil_tasks.yaml") or {}
        tasks_raw = {k: v for k, v in (tasks_raw or {}).items() if k.startswith("unveil_")}
        self.tasks_config = tasks_raw

    # Critical: stop CrewBase from mapping ALL tasks from any fallback YAML.
    def map_all_task_variables(self):
        return  # no-op; our runner doesn’t need CrewBase to wire tasks

    # ---------- Agents ----------
    @agent
    def file_analyst(self) -> Agent:
        return Agent(config=self.agents_config["unveil_file_analyst"], verbose=False)

    @agent
    def linker(self) -> Agent:
        return Agent(config=self.agents_config["unveil_linker"], verbose=False)

    @agent
    def architect(self) -> Agent:
        return Agent(config=self.agents_config["unveil_architect"], verbose=False)

    # ---------- Tasks (optional; harmless to keep) ----------
    @task
    def scan_and_summarize(self) -> Task:
        return Task(config=self.tasks_config.get("unveil_scan_and_summarize", {}))

    @task
    def cross_link(self) -> Task:
        return Task(config=self.tasks_config.get("unveil_cross_link", {}))

    @task
    def repo_narrative_and_render(self) -> Task:
        return Task(config=self.tasks_config.get("unveil_repo_narrative_and_render", {}))

    # ---------- Crew ----------
    @crew
    def unveil(self) -> Crew:
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
        )

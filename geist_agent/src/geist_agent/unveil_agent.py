# src/geist_agent/unveil_agent.py
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Iterable, Optional

from crewai import Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task
from crewai.agents.agent_builder.base_agent import BaseAgent

from geist_agent.utils import ReportUtils, PathUtils
from geist_agent.unveil_tools import (
    walk_files, chunk_file, static_imports, render_report
)

# ---------- data passed via pipeline ----------
@dataclass
class UnveilInputs:
    path: str
    include: List[str]
    exclude: List[str]
    exts: Optional[List[str]]
    max_files: int
    title: str

@CrewBase
class UnveilCrew:
    """Agentic 'unveil' crew for multi-language codebase understanding."""

    agents: List[BaseAgent]
    tasks: List[Task]

    # ---------- Agents ----------
    @agent
    def file_analyst(self) -> Agent:
        return Agent(
            config=self.agents_config["unveil_file_analyst"],  # in agents.yaml
            verbose=False,
        )

    @agent
    def linker(self) -> Agent:
        return Agent(
            config=self.agents_config["unveil_linker"],  # in agents.yaml
            verbose=False,
        )

    @agent
    def architect(self) -> Agent:
        return Agent(
            config=self.agents_config["unveil_architect"],  # in agents.yaml
            verbose=False,
        )

    # ---------- Tasks ----------
    @task
    def scan_and_summarize(self) -> Task:
        """
        Walks files, chunks, runs file-level LLM summaries and collects:
          { file -> {summary, api, suspects_deps, callers_guess} }
        Also computes static imports per file.
        """
        return Task(
            config=self.tasks_config["unveil_scan_and_summarize"],
        )

    @task
    def cross_link(self) -> Task:
        """
        Uses all file summaries + static imports to infer inter-file edges,
        map components, and reconcile ambiguous imports.
        """
        return Task(
            config=self.tasks_config["unveil_cross_link"],
        )

    @task
    def repo_narrative_and_render(self) -> Task:
        """
        Writes the final Markdown report with Mermaid graph, components, tables.
        """
        return Task(
            config=self.tasks_config["unveil_repo_narrative_and_render"],
        )

    # ---------- Crew ----------
    @crew
    def unveil(self) -> Crew:
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
        )

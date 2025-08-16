import os
from crewai import Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task
from crewai.agents.agent_builder.base_agent import BaseAgent
from typing import List
from geist_agent.utils import ReportUtils

@CrewBase
class ScryingAgent():
    """Scrying crew for divination and research operations"""

    agents: List[BaseAgent]
    tasks: List[Task]
    topic: str = ""  # Store topic for filename generation

    def set_topic(self, topic: str):
        """Set the topic for filename generation"""
        self.topic = topic

    @agent
    def researcher(self) -> Agent:
        return Agent(
            config=self.agents_config['researcher'],  # type: ignore[index]
            verbose=True
        )

    @agent
    def reporting_analyst(self) -> Agent:
        return Agent(
            config=self.agents_config['reporting_analyst'],  # type: ignore[index]
            verbose=True
        )

    @task
    def research_task(self) -> Task:
        return Task(
            config=self.tasks_config['research_task'],  # type: ignore[index]
        )

    @task
    def reporting_task(self) -> Task:
        # Generate filename using utility
        filename = ReportUtils.generate_filename(self.topic)
        # Use absolute path to avoid path traversal issues
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        full_path = os.path.join(project_root, 'reports', 'scrying_reports', filename)
        
        return Task(
            config=self.tasks_config['reporting_task'],  # type: ignore[index]
            output_file=full_path
        )

    @crew
    def scrying(self) -> Crew:
        """Creates the ScryingAgent crew"""
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True
        )
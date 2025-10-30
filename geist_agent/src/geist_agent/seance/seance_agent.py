# src/geist_agent/seance/seance_agent.py 
from __future__ import annotations

import os
import textwrap
from typing import List, Tuple, Optional
from crewai import Agent, Crew, Task, Process

# Try both import styles so we work across CrewAI versions
try:
    from crewai.llm import LLM  # CrewAI >= ~0.51
except Exception:  # pragma: no cover
    try:
        from crewai import LLM  # some versions expose it here
    except Exception:  # pragma: no cover
        LLM = None  # type: ignore


def _build_prompt(question: str, contexts: List[Tuple[str, str, int, int, str]]) -> str:
    blocks = []
    for (_cid, file, s, e, preview) in contexts:
        blocks.append(f"### {file}:{s}-{e}\n{preview}")

    prompt = textwrap.dedent(f"""
        You are an expert software assistant. You must answer ONLY using the provided code excerpts.
        If the excerpts are insufficient or off-topic, say exactly:
        "I don’t have enough on-topic context to answer. I would need: <list missing info>."
        Do NOT invent details. Do NOT change the question.

        Question:
        {question}

        Context:
        {'\n\n'.join(blocks)}

        Tasks:
        1) Restate the question in one short sentence to confirm scope.
        2) Provide a precise answer grounded in the excerpts above.
        3) End with a "Sources:" section listing file:line citations you used.
    """).strip()



class SeanceAgent:
    """
    CrewAI-backed synthesizer for Séance.

    Model resolution:
      SEANCE_MODEL / SEANCE_API_BASE  -> preferred overrides
      MODEL / API_BASE                -> global defaults (matches your other commands)
    """

    def __init__(self) -> None:
        pass

    def _resolve_model(self, model_override: Optional[str]) -> tuple[str, Optional[str]]:
        model = (
            model_override
            or os.getenv("SEANCE_MODEL")
            or os.getenv("MODEL")
            or "ollama/qwen2.5:7b-instruct"
        )
        api_base = os.getenv("SEANCE_API_BASE") or os.getenv("API_BASE")
        return model, api_base

    def answer(
        self,
        question: str,
        contexts: List[Tuple[str, str, int, int, str]],
        model: Optional[str] = None,
        verbose: bool = False,
    ) -> str:
        # honor the verbose flag for CrewAI
        if not verbose:
            # keep CrewAI quiet unless user asks for verbosity
            os.environ.setdefault("CREWAI_LOG_LEVEL", "ERROR")

        prompt = _build_prompt(question, contexts)
        model_id, api_base = self._resolve_model(model)

        llm_obj = None
        if LLM is not None:
            llm_obj = LLM(model=model_id, base_url=api_base) if api_base else LLM(model=model_id)

        code_answerer = Agent(
            role="Code Answerer",
            goal="Answer questions about the repository using provided code snippets and cite file:line sources.",
            backstory="A seasoned software engineer who grounds every answer in the provided excerpts.",
            verbose=verbose,   
            llm=llm_obj,
            model=model_id,
        )

        task = Task(
            description=prompt,
            expected_output="A concise, accurate answer with a final 'Sources:' section listing file:line citations.",
            agent=code_answerer,
        )

        crew = Crew(
            agents=[code_answerer],
            tasks=[task],
            process=Process.sequential,
            verbose=verbose,   
        )

        result = crew.kickoff()
        return str(result).strip()
# === FILE: src/geist_agent/seance/seance_agent.py ==============================
from __future__ import annotations

from typing import List, Tuple, Optional
import os
import textwrap

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
    return textwrap.dedent(f"""
    You are an expert software assistant. Answer the user's question using ONLY the provided code excerpts.
    Always cite the files and line ranges you used, like: file.py:10-35.

    Question:
    {question}

    Context:
    {'\n\n'.join(blocks)}

    Return a concise answer (bullets okay) followed by a "Sources:" section listing the citations you used.
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
    ) -> str:
        prompt = _build_prompt(question, contexts)
        model_id, api_base = self._resolve_model(model)

        # Build LLM object when available; otherwise pass model string so CrewAI picks env defaults.
        llm_obj = None
        if LLM is not None:
            llm_obj = LLM(model=model_id, base_url=api_base) if api_base else LLM(model=model_id)

        code_answerer = Agent(
            role="Code Answerer",
            goal="Answer questions about the repository using provided code snippets and cite file:line sources.",
            backstory="A seasoned software engineer who grounds every answer in the provided excerpts.",
            verbose=True,
            llm=llm_obj,     # CrewAI uses this when present
            model=model_id,  # …and this keeps us working on versions where llm isn't required
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
            verbose=False,
        )

        result = crew.kickoff()
        return str(result).strip()

# src/geist_agent/seance/seance_agent.py 
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
    You are an expert software assistant. Your ONLY job is to answer the user's question,
    and you must ground your answer strictly in the provided excerpts (no outside knowledge).

    RULES (follow all):
      1) Stay laser-focused on the exact question below. Do not change the task.
      2) Use ONLY information found in the Context excerpts.
      3) If the question asks to LIST / NAME / FIND, return a bullet list of findings.
      4) Do NOT write code or pseudo-code unless the question explicitly asks for code.
      5) If the answer cannot be found in the excerpts, say: "Not found in provided context."
      6) Keep the answer concise and specific. No restatements of the question.
      7) End with a "Sources:" section that lists only file:line ranges you actually used.

    Question:
    {question}

    Context:
    {'\n\n'.join(blocks)}

    Return format:
    - A short direct answer (bullets OK; no preamble).
    - Then a line "Sources:" and a bullet list of file:line citations.
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
            expected_output=(
                "A concise, directly on-task answer based ONLY on the provided Context.\n"
                "- If the question asks to LIST/NAME/FIND, provide a bullet list of findings only.\n"
                "- No code unless explicitly asked.\n"
                "- If not found in Context, respond exactly: 'Not found in provided context.'\n"
                "- End with:\n"
                "Sources:\n"
                "- file.py:10-35\n"
            ),
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

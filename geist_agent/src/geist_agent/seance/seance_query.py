# === FILE: seance_query.py ====================================================
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple, Optional
import json
import os
import textwrap

from .seance_index import load_manifest, index_path
from .seance_common import tokenize
from .seance_agent import SeanceAgent

# ─────────────────────────────── Retrieval ─────────────────────────────────────

def _score_jaccard(query_tokens: List[str], chunk_tokens: List[str]) -> float:
    a, b = set(query_tokens), set(chunk_tokens)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def retrieve(root: Path, name: str, query: str, k: int = 6) -> List[Tuple[str, float]]:
    """
    Return top-k (chunk_id, score) candidates against the inverted index.
    """
    man = load_manifest(root, name)
    if not man:
        raise RuntimeError(f"No manifest for seance '{name}'. Run `seance connect` & `seance index`.")

    ip = index_path(root, name)
    if not ip.exists():
        raise RuntimeError(f"No index found for seance '{name}'. Run `seance index`.")

    inverted: Dict[str, Dict[str, int]] = json.loads(ip.read_text(encoding="utf-8"))

    qtokens = tokenize(query)
    candidate_scores: Dict[str, int] = {}
    for qt in qtokens:
        postings = inverted.get(qt)
        if not postings:
            continue
        for cid, freq in postings.items():
            candidate_scores[cid] = candidate_scores.get(cid, 0) + freq

    ranked: List[Tuple[str, float]] = []
    # reconstruct approx token sets for each candidate (MVP)
    for cid in candidate_scores.keys():
        ctoks: List[str] = [t for t, posting in inverted.items() if cid in posting]
        score = _score_jaccard(qtokens, ctoks)
        ranked.append((cid, score))

    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked[:k]

# ─────────────────────────────── Answering ─────────────────────────────────────

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

def generate_answer(
    question: str,
    contexts: List[Tuple[str, str, int, int, str]],
    use_llm: bool = True,
    model: Optional[str] = None,
    timeout: int = 30,  # kept for signature compatibility; not used by CrewAI path
) -> Tuple[str, str, Optional[str]]:
    """
    LLM-first (CrewAI) answer generation. Falls back to extractive preview if disabled or errors.
    Returns: (answer_text, mode, reason)
      - mode: "llm" or "fallback"
      - reason: str for why we fell back (None when mode == "llm")
    """
    if not use_llm:
        return _fallback_answer(question, contexts), "fallback", "LLM disabled via flag"

    try:
        agent = SeanceAgent()
        txt = agent.answer(question=question, contexts=contexts, model=model)
        if txt and txt.strip():
            return txt, "llm", None
        return _fallback_answer(question, contexts), "fallback", "LLM returned empty content"
    except Exception as e:
        return _fallback_answer(question, contexts), "fallback", f"LLM error: {e.__class__.__name__}: {e}"

def _fallback_answer(question: str, contexts: List[Tuple[str, str, int, int, str]]) -> str:
    bullets = []
    for (_, file, s, e, preview) in contexts:
        snippet = preview.strip().splitlines()[:6]
        bullets.append(f"- {file}:{s}-{e}\n  " + "\n  ".join(snippet))
    src_lines = "\n".join(bullets)
    return (
        f"Q: {question}\n\n"
        f"Top findings (preview):\n"
        f"{src_lines}\n\n"
        f"(LLM was unavailable or disabled; showing extractive preview.)"
    )

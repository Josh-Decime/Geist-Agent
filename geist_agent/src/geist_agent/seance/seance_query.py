# src/geist_agent/seance/seance_query.py 
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
    Return top-k (chunk_id, score) candidates.
    Default retriever = BM25, configurable via SEANCE_RETRIEVER={bm25|jaccard}.
    """
    man = load_manifest(root, name)
    if not man:
        raise RuntimeError(f"No manifest for seance '{name}'. Run `seance connect` & `seance index`.")

    ip = index_path(root, name)
    if not ip.exists():
        raise RuntimeError(f"No index found for seance '{name}'. Run `seance index`.")

    inverted: Dict[str, Dict[str, int]] = json.loads(ip.read_text(encoding="utf-8"))

    qtokens = tokenize(query)
    retriever = (os.getenv("SEANCE_RETRIEVER") or "bm25").strip().lower()
    retriever = "bm25" if retriever not in ("bm25", "jaccard") else retriever

    if retriever == "bm25":
        # ----- BM25 -----
        doc_len: Dict[str, int] = {}
        for _term, postings in inverted.items():
            for cid, tf in postings.items():
                doc_len[cid] = doc_len.get(cid, 0) + int(tf)

        if not doc_len:
            return []

        N = len(doc_len)
        avgdl = (sum(doc_len.values()) / float(N)) if N > 0 else 1.0

        try:
            k1 = float(os.getenv("SEANCE_BM25_K1", "1.2"))
        except ValueError:
            k1 = 1.2
        try:
            b = float(os.getenv("SEANCE_BM25_B", "0.75"))
        except ValueError:
            b = 0.75

        import math
        idf: Dict[str, float] = {}
        for qt in set(qtokens):
            postings = inverted.get(qt)
            if not postings:
                continue
            n_t = len(postings)  # docs containing term
            idf[qt] = math.log(1.0 + (N - n_t + 0.5) / (n_t + 0.5))

        scores: Dict[str, float] = {}
        for qt in set(qtokens):
            postings = inverted.get(qt)
            if not postings:
                continue
            qt_idf = idf.get(qt, 0.0)
            for cid, tf in postings.items():
                dl = doc_len.get(cid, 1)
                denom = tf + k1 * (1.0 - b + b * (dl / avgdl))
                part = qt_idf * ((tf * (k1 + 1.0)) / denom)
                scores[cid] = scores.get(cid, 0.0) + part

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:k]

    # ----- Jaccard (legacy/simple) -----
    candidate_scores: Dict[str, int] = {}
    for qt in qtokens:
        postings = inverted.get(qt)
        if not postings:
            continue
        for cid, freq in postings.items():
            candidate_scores[cid] = candidate_scores.get(cid, 0) + freq

    ranked: List[Tuple[str, float]] = []
    for cid in candidate_scores.keys():
        ctoks: List[str] = [t for t, posting in inverted.items() if cid in posting]
        score = _score_jaccard(qtokens, ctoks)
        ranked.append((cid, score))

    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked[:k]

# ─────────────────────────────── Answering ─────────────────────────────────────

def generate_answer(
    question: str,
    contexts: List[Tuple[str, str, int, int, str]],
    use_llm: bool = True,
    model: Optional[str] = None,
    timeout: int = 30,          # kept for signature compatibility (unused here)
    verbose: bool = False,      # let runner control verbosity
) -> Tuple[str, str, Optional[str]]:
    """
    LLM-first answer generation via CrewAI (SeanceAgent). Falls back to extractive preview
    if LLM is disabled or in case of unexpected errors.
    Returns: (answer_text, mode, reason)
    """
    if not use_llm:
        return _fallback_answer(question, contexts), "fallback", "LLM disabled via flag"

    try:
        agent = SeanceAgent()
        txt = agent.answer(question=question, contexts=contexts, model=model, verbose=verbose)
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

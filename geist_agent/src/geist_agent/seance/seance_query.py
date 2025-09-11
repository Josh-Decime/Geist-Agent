# === FILE: seance_query.py ====================================================
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple
import json

from .seance_index import load_manifest, index_path
from .seance_common import tokenize

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

def generate_answer(query: str, contexts: List[Tuple[str, str, int, int, str]]) -> str:
    """
    Non-LLM, readable baseline answer: shows top findings and previews with citations.
    contexts: list of (chunk_id, file, start_line, end_line, preview)
    """
    bullets = []
    for (_, file, s, e, preview) in contexts:
        snippet = preview.strip().splitlines()[:6]
        bullets.append(f"- {file}:{s}-{e}\n  " + "\n  ".join(snippet))
    src_lines = "\n".join(bullets)
    return (
        f"Q: {query}\n\n"
        f"Top findings (preview):\n"
        f"{src_lines}\n\n"
        f"(Tip: enable a model in generate_answer() for synthesized summaries.)"
    )

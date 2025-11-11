# src/geist_agent/seance/seance_query.py
from __future__ import annotations

import re
import os
import json
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from .seance_index import load_manifest, index_path
from .seance_common import tokenize
from .seance_agent import SeanceAgent

# ─────────────────────────────── Retrieval ─────────────────────────────────────

def _score_jaccard(query_tokens: List[str], chunk_tokens: List[str]) -> float:
    """Legacy/simple Jaccard score (kept for compatibility)."""
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

    # Tokenize the user query
    qtokens = tokenize(query)

        # --- retrieval debug: show missing tokens and the index size ---
    if (os.getenv("SEANCE_RETRIEVAL_LOG", "").strip().lower() in ("1", "true", "yes", "on")):
        total_tokens = len(inverted)
        missing = [t for t in set(qtokens) if t not in inverted]
        if missing:
            print(f"• Missing in index: {missing} (index terms={total_tokens})")
        else:
            print(f"• All query tokens present (index terms={total_tokens})")


    # Optional debug logging (guarded by env)
    if (os.getenv("SEANCE_RETRIEVAL_LOG", "").strip().lower() in ("1", "true", "yes", "on")):
        print(f"• Tokens: {qtokens}")
        print("• Symbolish tokens:",
              [t for t in qtokens
               if "_" in t or (any(c.islower() for c in t) and any(c.isupper() for c in t)) or len(t) >= 12])

    retriever = (os.getenv("SEANCE_RETRIEVER") or "bm25").strip().lower()
    retriever = "bm25" if retriever not in ("bm25", "jaccard") else retriever

    # ----- BM25 (default) -----
    if retriever == "bm25":
        # Build doc lengths
        doc_len: Dict[str, int] = {}
        for _term, postings in inverted.items():
            for cid, tf in postings.items():
                doc_len[cid] = doc_len.get(cid, 0) + int(tf)

        if not doc_len:
            return []

        N = len(doc_len)
        avgdl = (sum(doc_len.values()) / float(N)) if N > 0 else 1.0

        # Tunables (BM25)
        try:
            k1 = float(os.getenv("SEANCE_BM25_K1", "1.2"))
        except ValueError:
            k1 = 1.2
        try:
            b = float(os.getenv("SEANCE_BM25_B", "0.75"))
        except ValueError:
            b = 0.75

        # IDF
        import math
        idf: Dict[str, float] = {}
        for qt in set(qtokens):
            postings = inverted.get(qt)
            if not postings:
                continue
            n_t = len(postings)  # docs containing term
            idf[qt] = math.log(1.0 + (N - n_t + 0.5) / (n_t + 0.5))

        # ---------- SYMBOL-AWARE IDF BOOST (makes code symbols dominate) ----------
        # Apply massive IDF boost to symbol-like tokens (underscores, CamelCase, long tokens)
        symbolish = [
            qt for qt in set(qtokens)
            if "_" in qt or len(qt) >= 12 or any(c.isupper() for c in qt if c.isalpha())
        ]
        symbol_boost = float(os.getenv("SEANCE_SYMBOL_IDF_BOOST", "100.0"))  # Configurable!

        for qt in symbolish:
            if qt in idf:
                original = idf[qt]
                idf[qt] = original * symbol_boost
                if os.getenv("SEANCE_RETRIEVAL_LOG", "").lower() in ("1", "true", "yes", "on"):
                    print(f"• IDF boost: '{qt}' {original:.3f} → {idf[qt]:.3f} (×{symbol_boost})")

        # Base BM25 scores
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

        # ---------- identifier-aware presence boost (helps code symbols) ----------
        # Treat underscore/camelCase/long tokens as code identifiers and boost their presence.
        symbolish: List[str] = []
        for qt in set(qtokens):
            if "_" in qt or (any(c.islower() for c in qt) and any(c.isupper() for c in qt)) or len(qt) >= 12:
                symbolish.append(qt)

        try:
            kw_boost = float(os.getenv("SEANCE_KEYWORD_BOOST", "6.0"))
        except Exception:
            kw_boost = 6.0

        presence: Dict[str, float] = {}
        for qt in set(qtokens):
            postings = inverted.get(qt)
            if not postings:
                continue
            weight = 2.0 if qt in symbolish else 1.0
            for cid, _tf in postings.items():
                presence[cid] = presence.get(cid, 0.0) + weight

        for cid, pres in presence.items():
            scores[cid] = scores.get(cid, 0.0) + pres * kw_boost
        
        # ---------- pattern-level boost (prefer defs and real call sites) ----------
        # We give extra weight when the symbol appears in:
        #   • a function definition:   def generate_filename(...)
        #   • a callsite:              generate_filename(…)  or  ReportUtils.generate_filename(…)
        try:
            pattern_boost = float(os.getenv("SEANCE_PATTERN_BOOST", "8.0"))
        except Exception:
            pattern_boost = 8.0

        # Load manifest to map chunk ids -> file/line ranges
        try:
            man = load_manifest(root, name)
        except Exception:
            man = None

        if man:
            # Candidate pool: chunks that contain ANY query token (esp. symbolish)
            candidate_cids: set[str] = set()
            basis = symbolish or list(set(qtokens))
            for qt in set(basis):
                postings = inverted.get(qt) or {}
                candidate_cids.update(postings.keys())

            # Lightweight parser helpers
            sym = next(iter(symbolish), None)
            sym = sym or (qtokens[0] if qtokens else None)
            if sym:
                # compile common regexes for this symbol
                # def generate_filename(...
                re_def = re.compile(rf"\bdef\s+{re.escape(sym)}\s*\(", re.IGNORECASE)
                # …generate_filename(… or ReportUtils.generate_filename(…
                re_call = re.compile(rf"(\b{re.escape(sym)}\s*\(|\b[A-Za-z_][A-Za-z0-9_]*\s*\.\s*{re.escape(sym)}\s*\()", re.IGNORECASE)

                for cid in list(candidate_cids):
                    meta = man.chunks.get(cid)
                    if not meta:
                        continue
                    fp = Path(root) / meta.file
                    try:
                        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                        text = "\n".join(lines[meta.start_line - 1: meta.end_line])
                    except Exception:
                        continue

                    # crude comment/docstring filter: downweight if symbol only shows in comments/strings
                    # (we still apply positive boosts, but try not to elevate doc-only mentions)
                    # Quick heuristic: count hits on non-comment lines.
                    code_hits = 0
                    total_hits = 0
                    for ln in text.splitlines():
                        if sym.lower() in ln.lower():
                            total_hits += 1
                            # consider line "code" if not starting with '#' and not obviously in a triple-quoted block start/end
                            stripped = ln.lstrip()
                            if not stripped.startswith("#"):
                                code_hits += 1

                    bonus = 0.0
                    if re_def.search(text):
                        bonus += pattern_boost * 1.5   # definition is strongest
                    if re_call.search(text):
                        bonus += pattern_boost * 1.0   # call site is strong

                    # If all mentions look like comments/doc, trim the bonus a bit
                    if total_hits > 0 and code_hits == 0:
                        bonus *= 0.5

                    if bonus > 0:
                        scores[cid] = scores.get(cid, 0.0) + bonus
        # ---------- /pattern-level boost ----------


        # Sort and guard-rail
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top = ranked[:k]

        # If none of the top-k actually contains ANY query token (rare), re-rank by symbol hits only
        def _chunk_has_any_query_token(_cid: str) -> bool:
            for qt in set(qtokens):
                if inverted.get(qt) and _cid in inverted[qt]:
                    return True
            return False

        if top and not any(_chunk_has_any_query_token(cid) for cid, _ in top):
            pool: Dict[str, float] = {}
            for qt in symbolish or set(qtokens):
                postings = inverted.get(qt) or {}
                for cid, tf in postings.items():
                    pool[cid] = pool.get(cid, 0.0) + float(tf)
            if pool:
                return sorted(pool.items(), key=lambda x: x[1], reverse=True)[:k]


        if (os.getenv("SEANCE_RETRIEVAL_LOG", "").strip().lower() in ("1", "true", "yes", "on")):
            # Map top candidates to files (best-effort)
            try:
                man = load_manifest(root, name)
                if man:
                    files = []
                    for cid, sc in top:
                        meta = man.chunks.get(cid)
                        if meta:
                            files.append(f"{meta.file}:{meta.start_line}-{meta.end_line} ({sc:.4f})")
                    if files:
                        print("• Top candidates:")
                        for line in files:
                            print("  -", line)
            except Exception as _e:
                pass

        return top

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
    timeout: int = 30,          # kept for signature compatibility (unused)
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
    for (_cid, file, s, e, preview) in contexts:
        snippet = preview.strip().splitlines()[:6]
        bullets.append(f"- {file}:{s}-{e}\n  " + "\n  ".join(snippet))
    src_lines = "\n".join(bullets)
    return (
        f"Q: {question}\n\n"
        f"Top findings (preview):\n"
        f"{src_lines}\n\n"
        f"(LLM was unavailable or disabled; showing extractive preview.)"
    )

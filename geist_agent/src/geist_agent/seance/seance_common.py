# src/geist_agent/seance/seance_common.py 
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional
import hashlib
import re

# ───────────────────────────── Supported filetypes ─────────────────────────────

SUPPORTED_CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".go",
    ".rs", ".cpp", ".c", ".h", ".hpp", ".rb", ".php", ".kt", ".swift",
}
SUPPORTED_TEXT_EXTS = {".md", ".rst", ".txt", ".ini", ".json", ".yaml", ".yml", ".toml"}
SUPPORTED_EXTS = SUPPORTED_CODE_EXTS | SUPPORTED_TEXT_EXTS

DEFAULT_MAX_CHARS_PER_CHUNK = 1200
DEFAULT_CHUNK_OVERLAP = 150  # approximate chars; converted to lines heuristically

# ────────────────────────────────── Data model ─────────────────────────────────

@dataclass
class Chunk:
    file: Path
    start_line: int
    end_line: int
    text: str
    id: str  # unique chunk id

# ────────────────────────────────── Utilities ──────────────────────────────────

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", "ignore"))

def file_hash(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except Exception:
        return ""  # unreadable files are skipped

def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTS

def should_ignore(path: Path, root: Path) -> bool:
    # Expand later to respect .gitignore; skip hidden dirs + .geist by default
    parts = path.relative_to(root).parts
    if any(p.startswith(".") and p not in (".",) for p in parts):
        return True
    if ".geist" in parts:
        return True
    return False

def read_text_safely(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

# ────────────────────────────── Tokenize & Chunk ───────────────────────────────

_WORD_RE = re.compile(r"[A-Za-z0-9_]+", re.UNICODE)

def tokenize(text: str) -> List[str]:
    return [m.group(0).lower() for m in _WORD_RE.finditer(text)]

def greedy_line_chunk(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS_PER_CHUNK,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> List[Tuple[int, int, str]]:
    """
    Greedy, line-preserving chunker with small overlap.
    Returns (start_line, end_line, chunk_text) using 1-based line numbers.
    """
    lines = text.splitlines()
    chunks: List[Tuple[int, int, str]] = []
    start = 0
    while start < len(lines):
        block = []
        length = 0
        i = start
        while i < len(lines) and length + len(lines[i]) + 1 <= max_chars:
            block.append(lines[i])
            length += len(lines[i]) + 1
            i += 1
        if not block:
            block = [lines[start][:max_chars]]
            i = start + 1
        chunk_text = "\n".join(block)
        # convert char overlap to approx line overlap (80 chars per line heuristic)
        overlap_lines = max(0, min(overlap // 80, i - start))
        next_start = (i - overlap_lines) if overlap_lines else i
        chunks.append((start + 1, i, chunk_text))
        start = max(next_start, i)
    return chunks

def make_chunk_id(file: Path, start_line: int, end_line: int, fh: str) -> str:
    # Keep it stable across runs, tied to file hash and line span
    return f"{fh}:{start_line}:{end_line}"

# src/geist_agent/seance/seance_index.py 
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path, PurePosixPath
import json
import time
import os
from typing import Dict, Optional, Iterable

from geist_agent.utils import PathUtils

from .seance_common import (
    is_supported, should_ignore, read_text_safely,
    tokenize, greedy_line_chunk, file_hash, make_chunk_id
)

# ────────────────────────────────── Data model ─────────────────────────────────

@dataclass
class IndexedChunk:
    id: str
    file: str
    start_line: int
    end_line: int
    text_hash: str  # coarse: file hash; can switch to per-chunk hash later

@dataclass
class Manifest:
    root: str
    created_at: float
    updated_at: float
    files: Dict[str, str]         # file -> file_hash
    chunks: Dict[str, IndexedChunk]  # chunk_id -> metadata

def _parse_list_env(var_name: str) -> list[str]:
    """Split a comma/space-separated env var into a clean lowercased list."""
    raw = os.getenv(var_name, "") or ""
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    return [p.lower() for p in parts if p]

def _normalize_exts(items: Iterable[str]) -> set[str]:
    """
    Normalize tokens to extensions (keep 'dockerfile' special).
    If token doesn't start with '.', prepend it.
    """
    out: set[str] = set()
    for it in items or []:
        if not it:
            continue
        if it.startswith(".") or it == "dockerfile":
            out.add(it)
        else:
            out.add("." + it)
    return out

def _env_filters():
    """
    Compute include/ext filters and ignore globs from env.
      - SEANCE_INCLUDE_EXTS ⇒ allowlist (wins)
      - SEANCE_EXCLUDE_EXTS ⇒ blocklist (applied if allowlist not set)
      - SEANCE_IGNORE_GLOBS ⇒ path globs to skip
    """
    include_exts = _normalize_exts(_parse_list_env("SEANCE_INCLUDE_EXTS"))
    exclude_exts = _normalize_exts(_parse_list_env("SEANCE_EXCLUDE_EXTS"))
    ignore_globs = _parse_list_env("SEANCE_IGNORE_GLOBS")
    return include_exts, exclude_exts, ignore_globs

def _skip_by_env(rel_posix: str, filename: str, ext: str,
                 include_exts: set[str], exclude_exts: set[str], ignore_globs: list[str]) -> bool:
    """
    Decide if a file should be skipped by env-based rules.
      - include_exts: if non-empty, only files whose ext/name is in it are allowed
      - exclude_exts: if ext/name is listed, skip
      - ignore_globs: any glob match on rel path ⇒ skip
    """
    # allow special filenames (like Dockerfile) by comparing basename lowercased
    special_name = filename.lower()

    # include allowlist takes precedence
    if include_exts:
        if ext in include_exts or special_name in include_exts:
            pass
        else:
            return True

    # exclude blocklist (only when include list not set)
    if not include_exts and (ext in exclude_exts or special_name in exclude_exts):
        return True

    if ignore_globs:
        p = PurePosixPath(rel_posix)
        for pat in ignore_globs:
            # match is case-sensitive on posix path; patterns are lowercased above
            # ensure we compare on lowercased string form
            if p.as_posix().lower().startswith("/") and not pat.startswith("/"):
                # not expected since we feed rel_posix; safe no-op
                pass
            if p.match(pat):
                return True

    return False


# ──────────────────────────────── Paths & IO ───────────────────────────────────

def seance_dir(root: Path, name: str) -> Path:
    """
    Store all séance artifacts outside the repo to avoid commits:
      ~/.geist/reports/seance/<name>/
    """
    base = PathUtils.ensure_reports_dir("seance")  # ~/.geist/reports/seance
    d = Path(base) / name
    d.mkdir(parents=True, exist_ok=True)
    return d

def manifest_path(root: Path, name: str) -> Path:
    return seance_dir(root, name) / "manifest.json"

def index_path(root: Path, name: str) -> Path:
    return seance_dir(root, name) / "inverted_index.json"

def load_manifest(root: Path, name: str) -> Optional[Manifest]:
    p = manifest_path(root, name)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    data["chunks"] = {k: IndexedChunk(**v) for k, v in data.get("chunks", {}).items()}
    return Manifest(**data)

def save_manifest(root: Path, name: str, manifest: Manifest) -> None:
    sd = seance_dir(root, name)
    sd.mkdir(parents=True, exist_ok=True)
    mp = manifest_path(root, name)
    payload = asdict(manifest)
    payload["chunks"] = {k: asdict(v) for k, v in manifest.chunks.items()}
    mp.write_text(json.dumps(payload, indent=2), encoding="utf-8")

# ──────────────────────────────── Operations ───────────────────────────────────

def connect(root: Path, name: str) -> Manifest:
    """
    Initialize a séance under .geist/seance/<name>/ if missing.
    """
    now = time.time()
    m = load_manifest(root, name)
    if m:
        return m
    m = Manifest(
        root=str(root),
        created_at=now,
        updated_at=now,
        files={},
        chunks={},
    )
    save_manifest(root, name, m)
    return m

def build_index(
    root: Path,
    name: str,
    max_chars: int = 1200,
    overlap: int = 150,
    verbose: bool = True
) -> None:
    """
    Walk files under `root`, (re)chunk changed ones, and update inverted index.
    """
    sr = Path(root).resolve()
    man = connect(sr, name)
    # Read env-based scan filters
    include_exts, exclude_exts, ignore_globs = _env_filters()

    # Load existing inverted index if present
    inverted: Dict[str, Dict[str, int]] = {}
    ip = index_path(sr, name)
    if ip.exists():
        try:
            inverted = json.loads(ip.read_text(encoding="utf-8"))
        except Exception:
            inverted = {}

    # Track chunks that remain valid after this build
    valid_chunks: Dict[str, bool] = {cid: False for cid in man.chunks.keys()}

    if verbose:
        print(f"▶ Scanning: {sr}")
        # One-time summary of env filters
        inc_msg = sorted(include_exts) if include_exts else None
        exc_msg = sorted(exclude_exts) if exclude_exts else []
        ign_msg = ignore_globs or []
        print(
            "• Filters: "
            f"include_exts={inc_msg if inc_msg is not None else 'DEFAULT'} | "
            f"exclude_exts={exc_msg} | "
            f"ignore_globs={ign_msg}"
        )
    count_files = 0
    updated_chunks = 0

    for p in sr.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(sr).as_posix()
        filename = p.name
        ext = p.suffix.lower()

        # Env-based skipping (include / exclude / globs)
        if _skip_by_env(rel, filename, ext, include_exts, exclude_exts, ignore_globs):
            continue

        # (existing checks)
        if not is_supported(p):
            continue
        if should_ignore(p, sr):
            continue

        count_files += 1

        fh = file_hash(p)
        prev_fh = man.files.get(rel)
        file_changed = fh != prev_fh

        if verbose:
            status = "changed" if file_changed else "cached"
            print(f"• {status:7} {rel}")

        if not file_changed:
            # mark all existing chunks for this file as valid
            for cid, meta in list(man.chunks.items()):
                if meta.file == rel:
                    valid_chunks[cid] = True
            continue

        # Read file
        text = read_text_safely(p)
        if text is None:
            if verbose:
                print(f"  ! skipped unreadable: {rel}")
            continue

        # Remove old chunks for this file
        for cid, meta in list(man.chunks.items()):
            if meta.file == rel:
                del man.chunks[cid]

        # Chunk & add to index
        lines = greedy_line_chunk(text, max_chars=max_chars, overlap=overlap)
        for (start_line, end_line, chunk_text) in lines:
            chash = fh  # coarse; could hash chunk_text for finer invalidation
            cid = make_chunk_id(p, start_line, end_line, fh)
            man.chunks[cid] = IndexedChunk(
                id=cid,
                file=rel,
                start_line=start_line,
                end_line=end_line,
                text_hash=chash,
            )
            valid_chunks[cid] = True
            updated_chunks += 1

            # Update inverted index
            for tok in tokenize(chunk_text):
                inverted.setdefault(tok, {}).setdefault(cid, 0)
                inverted[tok][cid] += 1

        # Update file hash
        man.files[rel] = fh

    # Prune stale chunks from manifest and inverted index
    for cid, ok in list(valid_chunks.items()):
        if not ok:
            if cid in man.chunks:
                del man.chunks[cid]
            for tok in list(inverted.keys()):
                if cid in inverted[tok]:
                    del inverted[tok][cid]
                if not inverted[tok]:
                    del inverted[tok]

    man.updated_at = time.time()
    save_manifest(sr, name, man)
    seance_dir(sr, name).mkdir(parents=True, exist_ok=True)
    ip.write_text(json.dumps(inverted), encoding="utf-8")

    if verbose:
        print(f"• Files scanned: {count_files}")
        print(f"• Chunks updated: {updated_chunks}")
        print(f"🪄  Seance index written: {ip}")

# === FILE: seance_index.py ====================================================
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional
import json
import time

from .seance_common import (
    Chunk, is_supported, should_ignore, read_text_safely,
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

# ──────────────────────────────── Paths & IO ───────────────────────────────────

def seance_dir(root: Path, name: str) -> Path:
    return root / ".geist" / "seance" / name

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
    count_files = 0
    updated_chunks = 0

    for p in sr.rglob("*"):
        if not p.is_file():
            continue
        if not is_supported(p):
            continue
        if should_ignore(p, sr):
            continue

        rel = p.relative_to(sr).as_posix()
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

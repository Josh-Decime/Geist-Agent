# === FILE: seance_session.py ==================================================
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional, Literal
import json
import time

Role = Literal["user", "assistant", "system"]

@dataclass
class SessionInfo:
    name: str
    root: str
    started_at: float
    transcript_md: str
    messages_jsonl: str
    meta_json: str
    k: int
    show_sources: bool

class SeanceSession:
    """
    Handles per-session files:
      - messages.jsonl  (stream of {"role","content","meta"})
      - transcript.md   (human-readable log)
      - session.json    (metadata)
    """

    def __init__(self, base_dir: Path, name: str, slug: str, k: int, show_sources: bool):
        ts = time.strftime("%Y%m%d-%H%M%S")
        folder = base_dir / "sessions" / f"{ts}_{slug}"
        folder.mkdir(parents=True, exist_ok=True)

        self.folder = folder
        self.messages_path = folder / "messages.jsonl"
        self.transcript_path = folder / "transcript.md"
        self.meta_path = folder / "session.json"

        self.info = SessionInfo(
            name=name,
            root=str(base_dir.parent.parent.parent),  # .geist/seance/<name>/ -> repo root
            started_at=time.time(),
            transcript_md=str(self.transcript_path),
            messages_jsonl=str(self.messages_path),
            meta_json=str(self.meta_path),
            k=k,
            show_sources=show_sources,
        )
        self._write_header()

    def _write_header(self):
        self.meta_path.write_text(json.dumps(asdict(self.info), indent=2), encoding="utf-8")
        header = [
            f"# Séance Session — {self.info.name}",
            "",
            f"- Started: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- k (top chunks): {self.info.k}",
            f"- show_sources: {self.info.show_sources}",
            "",
            "---",
            "",
        ]
        self.transcript_path.write_text("\n".join(header), encoding="utf-8")

    def append_message(self, role: Role, content: str, meta: Optional[Dict] = None):
        meta = meta or {}
        rec = {"ts": time.time(), "role": role, "content": content, "meta": meta}
        with self.messages_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        lines = []
        if role == "user":
            lines.append(f"**You:** {content}")
        elif role == "assistant":
            lines.append(f"**Seance:** {content}")
        else:
            lines.append(f"**{role.title()}:** {content}")

        if "sources" in meta and isinstance(meta["sources"], list) and meta["sources"]:
            lines.append("")
            lines.append("_Sources:_")
            for s in meta["sources"]:
                lines.append(f"- `{s}`")
        lines.append("")
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def set_k(self, k: int):
        self.info.k = k
        self._rewrite_meta()

    def set_show_sources(self, show: bool):
        self.info.show_sources = show
        self._rewrite_meta()

    def _rewrite_meta(self):
        self.meta_path.write_text(json.dumps(asdict(self.info), indent=2), encoding="utf-8")

    @property
    def paths(self):
        return {
            "folder": str(self.folder),
            "messages": str(self.messages_path),
            "transcript": str(self.transcript_path),
            "meta": str(self.meta_path),
        }

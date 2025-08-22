# src/geist_agent/unveil_tools.py
from __future__ import annotations
from pathlib import Path
from typing import Iterable, List, Dict, Tuple, Optional
import os, re, json

from geist_agent.utils import ReportUtils, PathUtils

DEFAULT_EXTS = {
    ".py",".js",".mjs",".cjs",".ts",".tsx",".jsx",".css",".html",".htm",
    ".java",".c",".h",".hpp",".hh",".cc",".cpp",".cs",".sql"
}
SKIP_DIRS = {".git",".hg",".svn",".venv","venv","__pycache__","node_modules",
             ".mypy_cache",".pytest_cache",".ruff_cache",".idea",".vscode",
             "dist","build",".egg-info","target","out"}

# ---------- Walk ----------
def walk_files(root: Path, include: Iterable[str], exclude: Iterable[str],
               exts: Optional[Iterable[str]], max_files: int) -> List[Path]:
    inc = [i.rstrip("/\\") for i in (include or [])]
    exc = [e.rstrip("/\\") for e in (exclude or [])]
    allow = set(e.lower() for e in (exts or [])) or DEFAULT_EXTS
    found: List[Path] = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not any((Path(cur)/d).as_posix().startswith(f"{root.as_posix()}/{ee}") for ee in exc)]
        for f in files:
            p = Path(cur, f)
            if p.suffix.lower() not in allow:
                continue
            rel = p.relative_to(root).as_posix()
            if any(rel.startswith(ee) for ee in exc):
                continue
            if inc and not any(rel.startswith(ii) for ii in inc):
                continue
            found.append(p)
            if len(found) >= max_files:
                return found
    return found

# ---------- Chunk ----------
def chunk_file(p: Path, max_chars: int = 6000) -> List[str]:
    txt = p.read_text(encoding="utf-8", errors="replace")
    # naive chunking (we can improve per language later)
    chunks = []
    cur = 0
    while cur < len(txt):
        chunks.append(txt[cur:cur+max_chars])
        cur += max_chars
    return chunks

# ---------- Static imports (seed signals) ----------
PY_IMPORT_RE = re.compile(r'^\s*(?:from\s+([a-zA-Z0-9_\.]+)\s+import|import\s+([a-zA-Z0-9_\.]+))', re.MULTILINE)
JS_IMPORT_RE = re.compile(r'^\s*import\s+.*?from\s+[\'"]([^\'"]+)[\'"]|^\s*import\s+[\'"]([^\'"]+)[\'"]|require\([\'"]([^\'"]+)[\'"]\)', re.MULTILINE)
C_CPP_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)

def static_imports(p: Path) -> List[str]:
    txt = p.read_text(encoding="utf-8", errors="replace")
    sfx = p.suffix.lower()
    if sfx == ".py":
        return [x for g in PY_IMPORT_RE.findall(txt) for x in g if x]
    if sfx in {".js",".mjs",".cjs",".ts",".tsx",".jsx"}:
        return [next((g for g in m.groups() if g), "") for m in JS_IMPORT_RE.finditer(txt) if next((g for g in m.groups() if g), "")]
    if sfx in {".c",".h",".hpp",".hh",".cc",".cpp"}:
        return [m.group(1) for m in C_CPP_INCLUDE_RE.finditer(txt)]
    # light: skip others for now (can add java/csharp/html/css/sql as needed)
    return []

# ---------- Mermaid + render ----------
def _mermaid(edges: List[Tuple[str,str]]) -> str:
    lines = ["```mermaid", "graph TD"]
    for a,b in edges:
        lines.append(f"  {a.replace('/','_').replace('.','_')} --> {b.replace('/','_').replace('.','_')}")
    lines.append("```")
    return "\n".join(lines)

def render_report(title: str,
                  root: Path,
                  file_summaries: Dict[str, Dict],
                  edges: List[Tuple[str,str]],
                  components: Dict[str, List[str]],
                  externals: Dict[str,int]) -> Path:
    md: List[str] = []
    md.append(f"# {title}\n")
    md.append(f"_Root_: `{root}`  ")
    md.append(f"_Files summarized_: **{len(file_summaries)}**\n")

    # High-level narrative (from architect agent already embedded in summaries)
    narrative = file_summaries.get("__repo__", {}).get("narrative", "")
    if narrative:
        md.append("## Overview\n")
        md.append(narrative.strip()+"\n")

    md.append("## Dependency Graph\n")
    md.append(_mermaid(edges))
    md.append("")

    if components:
        md.append("## Components\n")
        for comp, files in components.items():
            md.append(f"### {comp}\n")
            for f in sorted(files):
                md.append(f"- `{f}`")
            md.append("")
    if externals:
        md.append("## External Dependencies (inferred)\n")
        for dep, cnt in sorted(externals.items(), key=lambda x:-x[1])[:50]:
            md.append(f"- `{dep}` ×{cnt}")
        md.append("")
    md.append("## Inventory\n")
    md.append("| File | Role | Key API |\n|---|---|---|")
    for rel, d in sorted((k,v) for k,v in file_summaries.items() if k != "__repo__"):
        md.append(f"| `{rel}` | {d.get('role','-')} | {', '.join(d.get('api', [])[:6])} |")

    out_dir = PathUtils.ensure_reports_dir("code_maps")
    out_path = out_dir / ReportUtils.generate_filename("Unveil")
    out_path.write_text("\n".join(md), encoding="utf-8")
    return out_path

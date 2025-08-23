# src/geist_agent/unveil_tools.py
from __future__ import annotations
from pathlib import Path
from typing import Iterable, List, Dict, Tuple, Optional
import os, re, json
from collections import Counter, defaultdict

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

# ---------- Linking, graph & components ----------
def _resolve_token_to_file(token: str, all_files: list[Path], root: Path) -> Optional[str]:
    t = token.strip().rstrip(":")
    if not t:
        return None

    def _rel_if_exists(p: Path) -> Optional[str]:
        if p.exists():
            try:
                return p.relative_to(root).as_posix()
            except Exception:
                return p.as_posix()
        return None

    # ./ or ../ (JS/TS common)
    if t.startswith("./") or t.startswith("../"):
        cand = (root / t).resolve()
        for extra in ["", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".h", ".hpp", ".hh", ".cc", ".cpp"]:
            p = cand if extra == "" else cand.with_suffix(extra)
            rel = _rel_if_exists(p)
            if rel:
                return rel

    # Python dotted module → try under root and under root/src
    if "." in t and "/" not in t:
        mod = t.replace(".", "/")
        for base in [root, root / "src"]:
            for suff in [".py", "/__init__.py"]:
                guess = base / f"{mod}{suff}"
                rel = _rel_if_exists(guess)
                if rel:
                    return rel

    # Bare name → stem/filename match
    base = Path(t).name
    stem = Path(base).stem
    for p in all_files:
        if p.name == base:
            return p.relative_to(root).as_posix()
    for p in all_files:
        if p.stem == stem:
            return p.relative_to(root).as_posix()
    return None

def infer_edges_and_externals(root: Path, files: list[Path], static_map: dict[str, list[str]]) -> tuple[list[tuple[str,str]], dict[str,int]]:
    by_rel = {f.relative_to(root).as_posix(): f for f in files}
    edges: list[tuple[str,str]] = []
    externals = Counter()
    for rel, tokens in static_map.items():
        for tok in tokens:
            target = _resolve_token_to_file(tok, files, root)
            if target and target != rel:
                edges.append((rel, target))
            else:
                if not target:
                    externals[tok] += 1
    return edges, dict(externals)

def components_from_paths(files: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for rel in files:
        head = rel.split("/", 1)[0] if "/" in rel else rel
        groups[head].append(rel)
    return dict(groups)

def _mermaid(edges: List[Tuple[str,str]]) -> str:
    lines = ["```mermaid", "graph TD"]
    for a,b in edges:
        sa = a.replace('/','_').replace('.','_')
        sb = b.replace('/','_').replace('.','_')
        lines.append(f"  {sa} --> {sb}")
    lines.append("```")
    return "\n".join(lines)

def render_report(title: str, root: Path, file_summaries: Dict[str, Dict], edges: List[Tuple[str,str]], components: Dict[str, List[str]], externals: Dict[str,int]) -> Path:
    md: List[str] = []
    root_label = root.name  # hide full path
    md.append(f"# {title}\n")
    md.append(f"_Root_: `{root_label}`  ")
    md.append(f"_Files summarized_: **{len([k for k in file_summaries.keys() if k!='__repo__'])}**\n")

    # Overview first
    narrative = file_summaries.get("__repo__", {}).get("narrative", "")
    if narrative:
        md.append("## Overview\n")
        md.append(narrative.strip() + "\n")

    # Components next
    if components:
        md.append("## Components\n")
        for comp, files in sorted(components.items()):
            md.append(f"### {comp}\n")
            for f in sorted(files):
                md.append(f"- `{f}`")
            md.append("")

    # Dependency Graph
    md.append("## Dependency Graph\n")
    md.append(_mermaid(edges))
    md.append("")

    # File-by-file (rich)
    md.append("## Files\n")
    for rel, d in sorted((k,v) for k,v in file_summaries.items() if k != "__repo__"):
        md.append(f"### `{rel}`")
        role = d.get("role", "")
        api = d.get("api", []) or []
        summary = d.get("summary", []) or []
        if role:
            md.append(f"**Role:** {role}")
        if api:
            md.append(f"**API:** {', '.join(api[:12])}")
        if summary:
            md.append("**Summary:**")
            for line in summary[:8]:
                md.append(f"- {line}")
        md.append("")

    # Externals last
    if externals:
        md.append("## External Dependencies (inferred)\n")
        for dep, cnt in sorted(externals.items(), key=lambda x: -x[1])[:50]:
            md.append(f"- `{dep}` ×{cnt}")
        md.append("")

    out_dir = PathUtils.ensure_reports_dir("code_maps")
    out_path = out_dir / ReportUtils.generate_filename("Unveil")
    out_path.write_text("\n".join(md), encoding="utf-8")
    return out_path
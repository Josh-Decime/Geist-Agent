# src/geist_agent/unveil/unveil_tools.py
from __future__ import annotations
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional
from collections import Counter, defaultdict
from geist_agent.utils import ReportUtils, PathUtils
import re

# ---------- formatting helpers (API, summaries) ----------
def _format_api_list(api_val: Any, max_items: int = 12) -> list[str]:
    """Coerce various API representations (strings, dicts, mixed) to a list[str]."""
    if not isinstance(api_val, list):
        return []

    out: list[str] = []
    for item in api_val:
        if isinstance(item, str):
            out.append(item)
            continue

        if isinstance(item, dict):
            # Prefer a clean signature like: name(param1, param2)
            name = item.get("name") or item.get("function") or item.get("id")
            # accept either 'params' (dict) or 'parameters' (list/dict)
            params = item.get("params", item.get("parameters", []))

            param_names: list[str] = []
            if isinstance(params, dict):
                # e.g. {'as_json': {'type': 'bool', 'default': 'False'}}
                param_names = list(params.keys())
            elif isinstance(params, list):
                # e.g. [{'name':'topic','type':'str'}, ...] or just strings
                for p in params:
                    if isinstance(p, dict) and "name" in p:
                        param_names.append(str(p["name"]))
                    else:
                        param_names.append(str(p))

            if name:
                sig = f"{name}({', '.join(param_names)})" if param_names else str(name)
                out.append(sig)
            else:
                # Fallback: short repr
                out.append(str(item))
            continue

        # Unknown type (int/tuple/etc.): string it
        out.append(str(item))

        if len(out) >= max_items:
            break

    return out[:max_items]


def _format_summary_list(summary_val: Any, max_items: int = 8, max_len: int = 300) -> list[str]:
    """Coerce summary into a list[str] with sensible truncation."""
    if isinstance(summary_val, str):
        summary_list = [summary_val]
    elif isinstance(summary_val, list):
        summary_list = []
        for s in summary_val:
            if isinstance(s, str):
                summary_list.append(s)
            elif isinstance(s, dict):
                # Prefer typical keys if present
                text = s.get("text") or s.get("description") or s.get("summary") or str(s)
                summary_list.append(text)
            else:
                summary_list.append(str(s))
    else:
        summary_list = [str(summary_val)]

    # Trim overly long bullets
    out: list[str] = []
    for s in summary_list[:max_items]:
        s = s.strip()
        if len(s) > max_len:
            s = s[:max_len].rstrip() + "…"
        if s:
            out.append(s)
    return out

# ---------- chunking ----------
def chunk_file(p: Path, max_chars: int = 6000) -> List[str]:
    txt = p.read_text(encoding="utf-8", errors="replace")
    # naive chunking (we can improve per language later)
    chunks = []
    cur = 0
    while cur < len(txt):
        chunks.append(txt[cur:cur+max_chars])
        cur += max_chars
    return chunks

# ---------- static import patterns (regex) ----------
PY_IMPORT_RE = re.compile(r'^\s*(?:from\s+([a-zA-Z0-9_\.]+)\s+import|import\s+([a-zA-Z0-9_\.]+))', re.MULTILINE)
JS_IMPORT_RE = re.compile(r'^\s*import\s+.*?from\s+[\'"]([^\'"]+)[\'"]|^\s*import\s+[\'"]([^\'"]+)[\'"]|require\([\'"]([^\'"]+)[\'"]\)', re.MULTILINE)
C_CPP_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)

JAVA_IMPORT_RE   = re.compile(r'^\s*import\s+([a-zA-Z_][\w\.]*);', re.MULTILINE)
KOTLIN_IMPORT_RE = re.compile(r'^\s*import\s+([a-zA-Z_][\w\.]*)(?:\s+as\s+\w+)?', re.MULTILINE)
CSHARP_USING_RE  = re.compile(r'^\s*using\s+(?:static\s+)?([a-zA-Z_][\w\.]*)(?:\s*=\s*[a-zA-Z_][\w\.]*)?;', re.MULTILINE)

PHP_REQUIRE_RE   = re.compile(r'(?:require|include)(?:_once)?\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)', re.IGNORECASE)
RUBY_REQUIRE_RE  = re.compile(r'^\s*require(?:_relative)?\s*[\'"]([^\'"]+)[\'"]', re.MULTILINE)

CSS_IMPORT_RE    = re.compile(r'@import\s+(?:url\()?["\']?([^"\')]+)', re.IGNORECASE)
HTML_SRC_HREF_RE = re.compile(r'\b(?:src|href)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


# ---------- language helpers ----------
def _go_imports(txt: str) -> list[str]:
    tokens: list[str] = []
    # single-line: import "pkg/path"
    tokens += re.findall(r'^\s*import\s+"([^"]+)"', txt, re.MULTILINE)
    # block:
    for block in re.findall(r'^\s*import\s*\(\s*([\s\S]*?)\)\s*', txt, re.MULTILINE):
        tokens += re.findall(r'^\s*"([^"]+)"', block, re.MULTILINE)
    return tokens


# ---------- static import extraction ----------
def static_imports(p: Path) -> List[str]:
    txt = p.read_text(encoding="utf-8", errors="replace")
    sfx = p.suffix.lower()
    toks: list[str] = []

    if sfx == ".py":
        toks = [x for g in PY_IMPORT_RE.findall(txt) for x in g if x]

    elif sfx in {".js",".mjs",".cjs",".ts",".tsx",".jsx"}:
        toks = [next((g for g in m.groups() if g), "") for m in JS_IMPORT_RE.finditer(txt)
                if next((g for g in m.groups() if g), "")]

    elif sfx in {".c",".h",".hpp",".hh",".cc",".cpp"}:
        toks = [m.group(1) for m in C_CPP_INCLUDE_RE.finditer(txt)]

    elif sfx in {".java"}:
        toks = JAVA_IMPORT_RE.findall(txt)

    elif sfx in {".kt",".kts"}:
        toks = KOTLIN_IMPORT_RE.findall(txt)

    elif sfx == ".cs":
        toks = CSHARP_USING_RE.findall(txt)

    elif sfx == ".go":
        toks = _go_imports(txt)

    elif sfx == ".php":
        toks = PHP_REQUIRE_RE.findall(txt)

    elif sfx == ".rb":
        toks = RUBY_REQUIRE_RE.findall(txt)

    elif sfx == ".css":
        toks = CSS_IMPORT_RE.findall(txt)

    elif sfx in {".html",".htm"}:
        toks = HTML_SRC_HREF_RE.findall(txt)

    # normalize and dedupe while preserving order
    norm = []
    seen = set()
    for t in toks:
        t = t.replace("\\", "/").strip()
        if not t:
            continue
        if t not in seen:
            seen.add(t)
            norm.append(t)
    return norm


# ---------- Linking, graph & components ----------
def _resolve_token_to_file(token: str, all_files: list[Path], root: Path, source_file: Optional[Path] = None) -> Optional[str]:
    t = token.strip()
    if not t:
        return None
    # ignore URLs / non-file schemes
    tl = t.lower()
    if tl.startswith(("http://","https://","//","data:","mailto:","tel:","#")):
        return None
    t = t.replace("\\", "/").rstrip(":")

    def _rel_if_exists(p: Path) -> Optional[str]:
        if p.exists():
            try:
                return p.relative_to(root).as_posix()
            except Exception:
                return p.as_posix()
        return None

    def _resolve_path_candidate(cand: Path) -> Optional[str]:
        # direct file
        rel = _rel_if_exists(cand)
        if rel:
            return rel
        # try with common suffixes
        exts_try = ["", ".py",".ts",".tsx",".js",".jsx",".mjs",".cjs",".css",".html",".htm",
                    ".go",".rb",".php",".java",".kt",".kts",".c",".h",".hpp",".hh",".cc",".cpp",".cs"]
        for sx in exts_try:
            p2 = cand if sx == "" else cand.with_suffix(sx)
            rel = _rel_if_exists(p2)
            if rel:
                return rel
        # directory index (Node-ish)
        if cand.is_dir():
            for base in ["index.ts","index.tsx","index.js","index.jsx","index.mjs","index.cjs","__init__.py"]:
                rel = _rel_if_exists(cand / base)
                if rel:
                    return rel
        return None

    # 1) relative like ./ or ../ → from source file dir if possible
    if t.startswith("./") or t.startswith("../"):
        base = (source_file.parent if source_file else root)
        cand = (base / t).resolve()
        hit = _resolve_path_candidate(cand)
        if hit:
            return hit

    # 2) site-absolute like /assets/app.js → from repo root
    if t.startswith("/"):
        cand = (root / t.lstrip("/")).resolve()
        hit = _resolve_path_candidate(cand)
        if hit:
            return hit

    # 3) bare-ish path with slashes (e.g., "lib/util", "pkg/sub")
    if "/" in t:
        # try relative to source then root
        for base in ([source_file.parent] if source_file else []) + [root]:
            cand = (base / t).resolve()
            hit = _resolve_path_candidate(cand)
            if hit:
                return hit

    # 4) dotted module → try under common language roots
    if "." in t and "/" not in t:
        mod = t.replace(".", "/")
        lang_bases = [
            root / "src" / "main" / "java",
            root / "src" / "main" / "kotlin",
            root / "src",
            root
        ]
        for base in lang_bases:
            for suff in [".py",".java",".kt",".kts",".cs","/__init__.py"]:
                guess = base / f"{mod}{suff}"
                rel = _rel_if_exists(guess)
                if rel:
                    return rel

    # 5) last-segment rescue for dotted or slashed names
    last = t.split("/")[-1].split(".")[-1] if "/" in t else (t.split(".")[-1] if "." in t else t)
    if last:
        for p in all_files:
            if p.stem == last:
                try:
                    return p.relative_to(root).as_posix()
                except Exception:
                    return p.as_posix()

    # 6) bare filename (exact or stem)
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
        src_path = by_rel.get(rel)
        for tok in tokens:
            target = _resolve_token_to_file(tok, files, root, src_path)
            if target and target != rel:
                edges.append((rel, target))
            else:
                if not target:
                    externals[tok] += 1
    return edges, dict(externals)


# ---------- component grouping ----------
def components_from_paths(files: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for rel in files:
        head = rel.split("/", 1)[0] if "/" in rel else rel
        groups[head].append(rel)
    return dict(groups)


# ---------- graph labeling ----------
def _friendly_labels(paths: list[str]) -> dict[str, str]:
    """Make short, unique labels by escalating from filename -> parent/filename -> ..."""
    parts_map = {p: p.split("/") for p in paths}
    # start with just the filename
    depth = {p: 1 for p in paths}  # how many tail segments to show
    while True:
        labels = {p: "/".join(parts_map[p][-depth[p]:]) for p in paths}
        # count collisions
        counts = {}
        for lbl in labels.values():
            counts[lbl] = counts.get(lbl, 0) + 1
        collided = [p for p, lbl in labels.items() if counts[lbl] > 1]
        if not collided:
            return labels
        progressed = False
        for p in collided:
            if depth[p] < len(parts_map[p]):  # can add one more parent segment
                depth[p] += 1
                progressed = True
        if not progressed:
            # we've already promoted all the way to the full rel path; accept as-is
            return labels
        

# ---------- mermaid rendering ----------
def _mermaid(edges: List[Tuple[str, str]]) -> str:
    # Collect all nodes
    nodes = sorted({a for a, _ in edges} | {b for _, b in edges})
    # Build short, unique labels
    labels = _friendly_labels(nodes)

    def _id(rel: str) -> str:
        # Stable ID: keep using full rel path, sanitized (so edges remain deterministic)
        return rel.replace('/', '_').replace('.', '_')

    lines = ["```mermaid", "graph TD"]
    # Declare nodes with labels once
    for n in nodes:
        nid = _id(n)
        lbl = labels[n]
        # rectangular nodes with the short label
        lines.append(f'  {nid}["{lbl}"]')
    # Draw edges using the same IDs
    for a, b in edges:
        lines.append(f"  {_id(a)} --> {_id(b)}")
    lines.append("```")
    return "\n".join(lines)


# ---------- report rendering ----------
def render_report(
    title: str,
    root: Path,
    file_summaries: Dict[str, Dict],
    edges: List[Tuple[str, str]],
    components: Dict[str, List[str]],
    externals: Dict[str, int],
    reports_subfolder: str = "unveil_reports",
    filename_topic: Optional[str] = None,
) -> Path:
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
    if not edges:
        md.append("_No internal edges inferred (imports not resolved). " 
                "If this seems wrong, try running with --path pointing at the repo root._")
    md.append(_mermaid(edges))
    md.append("")

    # File-by-file (rich)
    md.append("## Files\n")
    for rel, d in sorted((k, v) for k, v in file_summaries.items() if k != "__repo__"):
        md.append(f"### `{rel}`")
        role = d.get("role", "")
        api = d.get("api", []) or []
        summary = d.get("summary", []) or []

        if role:
            md.append(f"**Role:** {role}")

        api_strs = _format_api_list(api)
        if api_strs:
            md.append(f"**API:** {', '.join(api_strs)}")

        summary_strs = _format_summary_list(summary)
        if summary_strs:
            md.append("**Summary:**")
            for line in summary_strs:
                md.append(f"- {line}")

        md.append("")

    # Externals last
    if externals:
        md.append("## External Dependencies (inferred)\n")
        for dep, cnt in sorted(externals.items(), key=lambda x: -x[1])[:50]:
            md.append(f"- `{dep}` ×{cnt}")
        md.append("")

    # --- Save report ---
    out_dir = PathUtils.ensure_reports_dir("unveil_reports")

    # Use repo root name (or fallback title) for filename
    root_label = root.name or "unknown_root"
    fname = ReportUtils.generate_filename(filename_topic or root_label)

    out_path = out_dir / fname
    out_path.write_text("\n".join(md), encoding="utf-8")
    return out_path



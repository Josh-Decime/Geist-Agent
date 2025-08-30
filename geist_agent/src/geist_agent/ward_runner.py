# src/geist_agent/ward_runner.py
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from contextlib import contextmanager

from geist_agent.utils import PathUtils
from geist_agent.unveil_tools import walk_files  # reuse your walker


# ---------- data types ----------
@dataclass
class Vuln:
    id: str
    ecosystem: str
    package: str
    version: str
    severity: str  # LOW/MEDIUM/HIGH/CRITICAL/UNKNOWN
    summary: str


@dataclass
class SecretHit:
    path: str
    line: int
    kind: str
    snippet: str  # "<redacted>" by default or masked preview if --preview


@dataclass
class Issue:
    path: str
    line: int
    rule: str
    snippet: str  # code context (non-secret), truncated


# ---------- helpers: exec/which ----------
def _which(prog: str) -> Optional[str]:
    return shutil.which(prog)


def _run(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    return p.returncode, out or "", err or ""


# ---------- LLM env profiles (per-tool .env overrides) ----------
_LLM_KEYS = [
    "MODEL", "API_BASE",
    "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
    "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
    "XAI_API_KEY", "COHERE_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY",
]

def _apply_prefixed_env(prefix: str):
    """Copy {PREFIX}_{KEY} into KEY (WARD_MODEL -> MODEL, etc.)."""
    for key in _LLM_KEYS:
        val = os.getenv(f"{prefix}_{key}")
        if val:
            os.environ[key] = val

@contextmanager
def _llm_profile(prefix: str):
    """Temporarily overlay env vars from a {PREFIX}_* profile."""
    original = {k: os.environ.get(k) for k in _LLM_KEYS}
    try:
        _apply_prefixed_env(prefix)
        yield
    finally:
        for k, v in original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------- helpers: manifest collection ----------
def _collect_manifests(root: Path) -> List[str]:
    names = {
        # Node
        "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "package.json",
        # Python
        "requirements.txt", "poetry.lock", "Pipfile.lock",
        # Go
        "go.mod", "go.sum",
        # Rust
        "Cargo.lock",
        # Java/Kotlin
        "pom.xml", "build.gradle", "build.gradle.kts",
        # Ruby
        "Gemfile.lock",
        # PHP
        "composer.lock",
        # .NET
        "packages.lock.json", "packages.config",
    }
    found: List[str] = []
    for cur, dirs, files in os.walk(root):
        base = cur.replace("\\", "/")
        if any(seg in base for seg in ("/node_modules", "/.git", "/.venv", "/venv", "/dist", "/build",
                                       "/.egg-info", "/target", "/out", "/.mypy_cache", "/.pytest_cache")):
            continue
        for f in files:
            if f in names:
                found.append(str(Path(cur, f)))
    return found


# ---------- scanners: dependency vulns via OSV-Scanner (optional) ----------
def _osv_scan(root: Path) -> List[Vuln]:
    exe = _which("osv-scanner")
    if not exe:
        return []

    manifests = _collect_manifests(root)
    if manifests:
        cmd = [exe, "--format=json", "--skip-git", *sum([["-L", m] for m in manifests], [])]
    else:
        cmd = [exe, "--format=json", "--skip-git", f"dir:{root}"]

    code, out, err = _run(cmd)
    if code != 0 or not (out.strip() or err.strip()):
        return []

    payload = out.strip() or err.strip()
    try:
        data = json.loads(payload)
    except Exception:
        return []

    vulns: List[Vuln] = []

    def _max_sev(sev_list: List[dict]) -> str:
        txt = json.dumps(sev_list).upper()
        if "CRITICAL" in txt: return "CRITICAL"
        if "HIGH" in txt:     return "HIGH"
        if "MEDIUM" in txt:   return "MEDIUM"
        if "LOW" in txt:      return "LOW"
        return "UNKNOWN"

    for r in data.get("results", []):
        for p in r.get("packages", []):
            pkg = p.get("package", {}) or {}
            name = pkg.get("name", "") or ""
            eco  = pkg.get("ecosystem", "") or ""
            versions = p.get("versions", []) or []
            vers = next((v for v in versions if v), "") or (versions[-1] if versions else "")
            for v in p.get("vulnerabilities", []) or []:
                sev = _max_sev(v.get("severity", []) or [])
                summary = v.get("summary") or v.get("details", "")[:140]
                vulns.append(Vuln(
                    id=v.get("id", "") or "",
                    ecosystem=eco,
                    package=name,
                    version=vers or "",
                    severity=sev,
                    summary=summary or ""
                ))
    return vulns


# ---------- scanners: secrets & insecure patterns ----------
_SECRET_PATTERNS: List[Tuple[str, str]] = [
    ("GitHub token", r"ghp_[A-Za-z0-9]{36,}"),
    ("Slack token", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("OpenAI key", r"sk-[A-Za-z0-9]{20,}"),
    ("AWS access key", r"AKIA[0-9A-Z]{16}"),
    ("Google API key", r"AIza[0-9A-Za-z\-_]{35}"),
    ("JWT", r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("Private key header", r"-----BEGIN (?:RSA|EC|OPENSSH|DSA|PGP) PRIVATE KEY-----"),
]

_INSECURE_PATTERNS: List[Tuple[str, str]] = [
    ("JS eval", r"\beval\s*\("),
    ("JS Function ctor", r"\bnew\s+Function\s*\("),
    ("Node exec", r"child_process\.(?:exec|execSync)\s*\("),
    ("Axios http", r"axios\([^)]*?\bhttp://"),
    ("Python shell=True", r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True"),
    ("Requests verify=False", r"requests\.[A-Za-z_]+\([^)]*verify\s*=\s*False"),
    ("Wildcard CORS", r"Access-Control-Allow-Origin['\"]?\s*[:=]\s*['\"]\*['\"]"),
    ("Debug true", r"\bDEBUG\s*=\s*True\b|\bprocess\.env\.NODE_ENV\s*!==\s*['\"]production['\"]"),
]


def _read_lines(path: Path) -> List[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []


def _masked_preview(s: str, start: int, end: int, keep: int = 3) -> str:
    m = s[start:end]
    if len(m) <= keep * 2:
        return "<redacted>"
    return f"{m[:keep]}…{m[-keep:]}"


def scan_secrets_and_issues(
    files: List[Path],
    root: Path,
    redact: bool = True,
    preview: bool = False,
    risky_context: int = 80,
) -> Tuple[List[SecretHit], List[Issue]]:
    hits: List[SecretHit] = []
    issues: List[Issue] = []
    for p in files:
        if p.suffix.lower() in {".map", ".min.js", ".min.css"}:
            continue
        lines = _read_lines(p)
        rel = p.relative_to(root).as_posix()

        for i, line in enumerate(lines, 1):
            sline = line.strip()

            for kind, rx in _SECRET_PATTERNS:
                m = re.search(rx, sline)
                if m:
                    snippet = "<redacted>"
                    if not redact:
                        snippet = sline[max(0, m.start()-4):min(len(sline), m.end()+4)][:80]
                    elif preview:
                        snippet = _masked_preview(sline, m.start(), m.end(), keep=3)
                    hits.append(SecretHit(path=rel, line=i, kind=kind, snippet=snippet))

            for rule, rx in _INSECURE_PATTERNS:
                m = re.search(rx, sline)
                if m:
                    start = max(0, m.start() - 8)
                    end   = min(len(sline), m.end() + 8)
                    issues.append(Issue(
                        path=rel,
                        line=i,
                        rule=rule,
                        snippet=sline[start:end][:risky_context]
                    ))
    return hits, issues


# ---------- LLM advisor (default ON; per-tool .env profile WARD_*) ----------
def _get_ward_advisor():
    """
    Try to build a CrewAI Agent for recommendations.
    Uses your current env (after profile overlay).
    """
    try:
        from crewai import Agent
        return Agent(
            role="Security Advisor",
            goal=("Given dependency CVEs, secrets, and risky patterns, produce prioritized remediation steps "
                  "with concise code/config examples."),
            backstory="Pragmatic AppSec engineer focused on high-signal fixes.",
            verbose=False,
            max_iter=1,
            cache=True,
            max_execution_time=60,
            respect_context_window=True,
        )
    except Exception:
        return None


def _llm_recommendations_with(advisor, vulns: List[Vuln], secrets: List[SecretHit], issues: List[Issue]) -> str:
    if advisor is None:
        return ""
    try:
        from crewai import Task

        def _shorten(vs: List[Vuln], n: int = 50):
            order = {"CRITICAL":4,"HIGH":3,"MEDIUM":2,"LOW":1,"UNKNOWN":0}
            vs = sorted(vs, key=lambda x: (-order.get(x.severity,0), x.ecosystem, x.package))
            return [
                {"id": v.id, "sev": v.severity, "pkg": f"{v.ecosystem}:{v.package}@{v.version}", "sum": v.summary[:140]}
                for v in vs[:n]
            ]

        payload = {
            "vulns": _shorten(vulns),
            "secrets": [{"kind": s.kind, "path": s.path, "line": s.line} for s in secrets[:50]],
            "issues": [{"rule": r.rule, "path": r.path, "line": r.line, "snippet": r.snippet} for r in issues[:80]],
        }

        prompt = (
            "Produce a short actionable security plan for this repository.\n"
            "Respond in Markdown with:\n"
            "1) **Top Priorities (24–48h)** — bullets with rationale.\n"
            "2) **Concrete Fixes** — short code or config examples per issue type.\n"
            "3) **Hardening Next** — follow-ups for the next week.\n"
            "Be specific and concise. Avoid boilerplate. Prefer code-level suggestions.\n\n"
            f"Findings JSON:\n{json.dumps(payload, indent=2)}"
        )
        task = Task(description=prompt, expected_output="Markdown with the specified sections.")
        out = advisor.execute_task(task)
        return str(out).strip()
    except Exception:
        return ""


# ---------- summarize & render ----------
def _sev_counts(vulns: List[Vuln]) -> Dict[str, int]:
    c = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}
    for v in vulns:
        c[v.severity] = c.get(v.severity, 0) + 1
    return c


def render_ward_markdown(
    title: str,
    root: Path,
    vulns: List[Vuln],
    secrets: List[SecretHit],
    issues: List[Issue],
    recommendations_md: str = "",
) -> str:
    sev = _sev_counts(vulns)
    lines: List[str] = []
    lines.append(f"# {title}\n")
    lines.append(f"_Root_: `{root.name}`  ")
    lines.append(f"Findings: **{len(vulns)} vulns** (C:{sev['CRITICAL']} H:{sev['HIGH']} M:{sev['MEDIUM']} L:{sev['LOW']}), "
                 f"**{len(secrets)} secrets**, **{len(issues)} risky patterns**\n")

    if vulns:
        lines.append("## Top Vulnerabilities")
        weight = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}
        for v in sorted(vulns, key=lambda x: (-weight.get(x.severity, 0), x.ecosystem, x.package))[:12]:
            pkg = f"{v.ecosystem}:{v.package}@{v.version}" if v.package else v.ecosystem
            lines.append(f"- `{v.id}` **{v.severity}** — {pkg} — {v.summary[:140]}")
        lines.append("")

    if secrets:
        lines.append("## Secrets (first 12)")
        for s in secrets[:12]:
            lines.append(f"- {s.kind} — `{s.path}:{s.line}` — {s.snippet}")
        lines.append("")

    if issues:
        lines.append("## Risky Patterns (first 12)")
        for r in issues[:12]:
            lines.append(f"- {r.rule} — `{r.path}:{r.line}` — {r.snippet}")
        lines.append("")

    if recommendations_md:
        lines.append("## Recommendations\n")
        lines.append(recommendations_md.strip())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def save_ward_json(root: Path, vulns: List[Vuln], secrets: List[SecretHit], issues: List[Issue]) -> Path:
    out_dir = PathUtils.ensure_reports_dir("ward_reports")
    out_json = out_dir / f"ward_{int(time.time())}.json"
    payload = {
        "root": str(root),
        "vulns": [v.__dict__ for v in vulns],
        "secrets": [s.__dict__ for s in secrets],
        "issues": [r.__dict__ for r in issues],
        "generated_at": int(time.time()),
    }
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_json


# ---------- command entry ----------
def run_ward(
    path: str,
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    exts: Optional[List[str]] = None,
    max_files: int = 3000,
    verbose: bool = True,
    use_osv: bool = True,
    redact: bool = True,
    preview: bool = False,
    llm: bool = True,  # LLM ON by default
) -> Path:
    root = Path(path).resolve()
    files = walk_files(root, include or [], exclude or [], [e.lower() for e in (exts or [])] or None, max_files)

    if verbose:
        print(f"[ward] scanning {len(files)} files under {root}", file=sys.stderr)

    vulns: List[Vuln] = _osv_scan(root) if use_osv else []
    secrets, issues = scan_secrets_and_issues(files, root, redact=redact, preview=preview, risky_context=80)

    recommendations_md = ""
    if llm:
        # If you have a loader, let it hydrate env; otherwise harmless no-op.
        try:
            from geist_agent.utils import EnvUtils
            if hasattr(EnvUtils, "load_env_for_tool"):
                EnvUtils.load_env_for_tool()
        except Exception:
            pass

        # Apply WARD_* env overlay just for the LLM work
        with _llm_profile("WARD"):
            advisor = _get_ward_advisor()
            recommendations_md = _llm_recommendations_with(advisor, vulns, secrets, issues)

    out_dir = PathUtils.ensure_reports_dir("ward_reports")
    out_md = out_dir / f"ward_{int(time.time())}.md"
    out_json = save_ward_json(root, vulns, secrets, issues)

    md = render_ward_markdown("Ward: Security Audit", root, vulns, secrets, issues, recommendations_md=recommendations_md)
    out_md.write_text(md, encoding="utf-8")

    if verbose:
        sev = _sev_counts(vulns)
        print(f"[ward] vulns: {len(vulns)} (C:{sev['CRITICAL']} H:{sev['HIGH']} M:{sev['MEDIUM']} L:{sev['LOW']})", file=sys.stderr)
        print(f"[ward] secrets: {len(secrets)}, risky patterns: {len(issues)}", file=sys.stderr)
        print(f"[ward] wrote: {out_md}", file=sys.stderr)
        print(f"[ward] json:  {out_json}", file=sys.stderr)

    return out_md


def main():
    ap = argparse.ArgumentParser("poltergeist ward")
    ap.add_argument("-p", "--path", required=True, help="Project root to audit")
    ap.add_argument("--max-files", type=int, default=3000)
    ap.add_argument("--include", action="append", default=[], help="Prefix filters (repeatable)")
    ap.add_argument("--exclude", action="append", default=[], help="Prefix filters (repeatable)")
    ap.add_argument("--ext", action="append", default=[], help="Allowed extensions (repeatable)")
    ap.add_argument("-q", "--quiet", action="store_true", help="Suppress progress logs")

    ap.add_argument("--no-osv", action="store_true", help="Disable OSV dependency scan even if osv-scanner is present")
    ap.add_argument("--no-redact", action="store_true", help="Do NOT redact secrets in report (discouraged)")
    ap.add_argument("--preview", action="store_true", help="Show masked preview for secrets (first/last 3 chars)")
    ap.add_argument("--no-llm", action="store_true", help="Disable LLM recommendations (on by default)")

    args = ap.parse_args()

    run_ward(
        path=args.path,
        include=args.include or None,
        exclude=args.exclude or None,
        exts=args.ext or None,
        max_files=args.max_files,
        verbose=not args.quiet,
        use_osv=not args.no_osv,
        redact=not args.no_redact,
        preview=bool(args.preview),
        llm=not args.no_llm,
    )


if __name__ == "__main__":
    main()

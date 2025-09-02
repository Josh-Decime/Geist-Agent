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
import urllib.request
import urllib.error
from collections import defaultdict, Counter


from geist_agent.utils import PathUtils
from geist_agent.unveil_tools import walk_files  

# ---------- tiny logger ----------
def _log(enabled: bool, msg: str):
    if enabled:
        print(msg, file=sys.stderr, flush=True)


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

# ---------- severity helpers (shared) ----------
SEV_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}
def _sev_sort_key(v: Vuln):
    return (-SEV_ORDER.get(v.severity, 0), v.ecosystem, v.package)

def _max_sev_from_list(sev_list: List[dict]) -> str:
    """
    OSV 'severity' may contain CVSS entries:
      [{"type":"CVSS_V3","score":"9.8"}] or [{"type":"CVSS_V3","score":"CVSS:3.1/AV:N/.../C:H/I:H/A:H"}]
    We map by numeric score if available; otherwise fall back to token matching.
    """
    buckets = []
    for entry in sev_list or []:
        score = entry.get("score")
        if isinstance(score, (int, float)):
            s = float(score)
        elif isinstance(score, str):
            # try plain float; if it's a vector string, sniff known tokens
            try:
                s = float(score)
            except ValueError:
                s = None
        else:
            s = None

        if s is not None:
            if s >= 9.0:      buckets.append("CRITICAL")
            elif s >= 7.0:    buckets.append("HIGH")
            elif s >= 4.0:    buckets.append("MEDIUM")
            elif s > 0.0:     buckets.append("LOW")
            continue

        # textual fallback (rare)
        val = json.dumps(entry).upper()
        if "CRITICAL" in val: buckets.append("CRITICAL")
        elif "HIGH" in val:   buckets.append("HIGH")
        elif "MODERATE" in val or "MEDIUM" in val: buckets.append("MEDIUM")
        elif "LOW" in val:    buckets.append("LOW")

    if "CRITICAL" in buckets: return "CRITICAL"
    if "HIGH" in buckets:     return "HIGH"
    if "MEDIUM" in buckets:   return "MEDIUM"
    if "LOW" in buckets:      return "LOW"
    return "UNKNOWN"


def _best_severity_from_osv_payload(osv_obj: dict) -> str:
    """
    Try 'severity' list; then GitHub's database_specific.severity (LOW/MODERATE/HIGH/CRITICAL).
    """
    sev = _max_sev_from_list(osv_obj.get("severity") or [])
    if sev != "UNKNOWN":
        return sev

    db = osv_obj.get("database_specific") or {}
    name = (db.get("severity") or "").upper()
    if name in ("LOW", "MODERATE", "MEDIUM", "HIGH", "CRITICAL"):
        return "MEDIUM" if name == "MODERATE" else name

    return "UNKNOWN"



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

# === OSV API FALLBACK: dependency collectors ===
def _collect_pinned_deps_for_osv(root: Path) -> List[Dict[str, str]]:
    """
    Return a list of dicts: {"ecosystem": "PyPI|npm", "name": "...", "version": "..."}
    Only collects *pinned* versions so OSV can give deterministic answers.
    Supports:
      - Python: requirements*.txt, pyproject.toml (tomllib if available)
      - Node: package-lock.json (preferred), package.json (pinned only)
    """
    deps: List[Dict[str, str]] = []

    # --- Python: requirements*.txt ---
    for req in root.rglob("requirements*.txt"):
        try:
            for line in req.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if "==" in s:  # pkg==1.2.3
                    name, ver = s.split("==", 1)
                    name = name.split("[", 1)[0].strip()
                    ver = ver.strip()
                    if name and ver:
                        deps.append({"ecosystem": "PyPI", "name": name, "version": ver})
        except Exception:
            pass

    # --- Python: pyproject.toml (requires python 3.11+ tomllib) ---
    try:
        import tomllib  # type: ignore
        for pp in root.rglob("pyproject.toml"):
            try:
                data = tomllib.loads(pp.read_text(encoding="utf-8"))
            except Exception:
                continue

            # PEP 621 deps
            for spec in (data.get("project", {}).get("dependencies") or []):
                name, ver = _pep_dep_exact(spec)
                if name and ver:
                    deps.append({"ecosystem": "PyPI", "name": name, "version": ver})

            # optional-dependencies
            for _group, items in (data.get("project", {}).get("optional-dependencies") or {}).items():
                for spec in items or []:
                    name, ver = _pep_dep_exact(spec)
                    if name and ver:
                        deps.append({"ecosystem": "PyPI", "name": name, "version": ver})

            # Poetry block (tool.poetry.dependencies)
            poetry = data.get("tool", {}).get("poetry", {})
            for name, spec in (poetry.get("dependencies") or {}).items():
                if name.lower() == "python":
                    continue
                ver = _poetry_exact_version(spec)
                if name and ver:
                    deps.append({"ecosystem": "PyPI", "name": name, "version": ver})
    except ModuleNotFoundError:
        pass

    # --- Node: package-lock.json (npm v7+ format preferred) ---
    for lock in root.rglob("package-lock.json"):
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
            pkgs = data.get("packages") or {}
            # npm v7+ writes "packages": { "node_modules/pkg": { "version": "x.y.z" }, ... }
            for path_key, entry in pkgs.items():
                if not isinstance(entry, dict):
                    continue
                if path_key and path_key.startswith("node_modules/"):
                    name = path_key.split("/", 1)[1]
                    ver = entry.get("version")
                    if name and ver:
                        deps.append({"ecosystem": "npm", "name": name, "version": ver})
        except Exception:
            pass

    # --- Node: package.json (only keep fully pinned) ---
    for pj in root.rglob("package.json"):
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
            for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                m = data.get(section) or {}
                for name, spec in m.items():
                    ver = _npm_semver_exact(spec)
                    if name and ver:
                        deps.append({"ecosystem": "npm", "name": name, "version": ver})
        except Exception:
            pass

    # dedupe
    seen = set()
    uniq: List[Dict[str, str]] = []
    for d in deps:
        key = (d["ecosystem"], d["name"], d["version"])
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


def _pep_dep_exact(spec: str) -> Tuple[str, Optional[str]]:
    """Return (name, exact_version|None) for PEP 508-ish specs (only '==')."""
    s = (spec or "").strip().split(";", 1)[0]
    name = s.split("[", 1)[0].strip()
    if "==" in s:
        return name, s.split("==", 1)[1].strip()
    return name, None


def _poetry_exact_version(spec) -> Optional[str]:
    """Return exact version when Poetry dep is a concrete string or {version: 'x.y.z'}."""
    if isinstance(spec, str):
        return spec if spec[:1].isdigit() else None
    if isinstance(spec, dict):
        v = spec.get("version")
        return v if isinstance(v, str) and v[:1].isdigit() else None
    return None


def _npm_semver_exact(spec: str) -> Optional[str]:
    """Only accept exact x.y.z (ignore ^ ~ ranges etc.)."""
    s = (spec or "").strip()
    s = s.lstrip("^~").split("||")[0].strip()
    import re as _re
    return s if _re.fullmatch(r"\d+\.\d+\.\d+(?:[-+].+)?", s) else None


# ---------- scanners: dependency vulns via OSV-Scanner (optional) ----------
def _osv_scan(root: Path, verbose: bool = False) -> List[Vuln]:
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

    for r in data.get("results", []):
        for p in r.get("packages", []):
            pkg = p.get("package", {}) or {}
            name = pkg.get("name", "") or ""
            eco  = pkg.get("ecosystem", "") or ""
            versions = p.get("versions", []) or []
            vers = next((v for v in versions if v), "") or (versions[-1] if versions else "")
            for v in p.get("vulnerabilities", []) or []:
                sev = _max_sev_from_list(v.get("severity", []) or [])
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

# === OSV API FALLBACK: API scanner ===
def _osv_api_scan(root: Path, verbose: bool = True) -> List[Vuln]:
    """
    Manual OSV check using https://api.osv.dev/v1/querybatch
    Builds queries from pinned dependency manifests discovered in the repo.
    """
    _log(verbose, "• Collecting pinned dependencies for OSV API…")
    deps = _collect_pinned_deps_for_osv(root)
    if not deps:
        _log(verbose, "  ← No pinned deps found to query; skipping OSV API.")
        return []

    payload = {
        "queries": [
            {"package": {"name": d["name"], "ecosystem": d["ecosystem"]}, "version": d["version"]} for d in deps
        ]
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url="https://api.osv.dev/v1/querybatch",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    _log(verbose, f"• Contacting OSV API with {len(deps)} queries…")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            obj = json.loads(raw)
    except urllib.error.URLError as e:
        _log(verbose, f"  ! OSV API network error: {e}")
        return []
    except Exception as e:
        _log(verbose, f"  ! OSV API error: {e}")
        return []

    findings: List[Vuln] = []
    for d, res in zip(deps, obj.get("results", [])):
        vulns = res.get("vulns") or []
        for v in vulns:
            findings.append(Vuln(
                id=v.get("id", "") or "",
                ecosystem=d["ecosystem"],
                package=d["name"],
                version=d["version"],
                severity=_max_sev_from_list(v.get("severity", []) or []),
                summary=(v.get("summary") or v.get("details", "")[:140]) or "",
            ))

    _log(verbose, f"  ← OSV API complete: {len(findings)} vulns")
    return findings

def _enrich_vulns_with_details(vulns: List[Vuln], *, limit: Optional[int] = None, verbose: bool = False) -> None:
    """
    For each vuln with UNKNOWN severity or empty summary, GET https://api.osv.dev/v1/vulns/{id}
    and update in-place. 'limit' caps network calls (env WARD_OSV_DETAILS_LIMIT used if None).
    """
    need = [v for v in vulns if v.id and (v.severity == "UNKNOWN" or not v.summary)]
    if not need:
        return

    cap = int(os.getenv("WARD_OSV_DETAILS_LIMIT", "120"))
    if limit is None:
        limit = cap

    done = 0
    for v in need:
        if limit is not None and done >= limit:
            break
        try:
            url = f"https://api.osv.dev/v1/vulns/{v.id}"
            req = urllib.request.Request(url=url, method="GET")
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", "replace")
                obj = json.loads(raw)

            sev = _best_severity_from_osv_payload(obj)
            if sev and sev != "UNKNOWN":
                v.severity = sev
            if not v.summary:
                v.summary = (obj.get("summary") or obj.get("details") or "")[:200]

            done += 1
            if verbose and done % 20 == 0:
                _log(True, f"  … OSV details enriched: {done}")
            time.sleep(0.05)  # polite throttle
        except Exception as e:
            if verbose:
                _log(True, f"  ! OSV details fetch failed for {v.id}: {e}")
            continue



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

# Pre-compile regexes (same behavior, faster)
_SECRET_REGEXES: List[Tuple[str, re.Pattern]] = [(k, re.compile(rx)) for k, rx in _SECRET_PATTERNS]
_INSECURE_REGEXES: List[Tuple[str, re.Pattern]] = [(k, re.compile(rx)) for k, rx in _INSECURE_PATTERNS]


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

            for kind, rx in _SECRET_REGEXES:
                m = rx.search(sline)
                if m:
                    snippet = "<redacted>"
                    if not redact:
                        snippet = sline[max(0, m.start()-4):min(len(sline), m.end()+4)][:80]
                    elif preview:
                        snippet = _masked_preview(sline, m.start(), m.end(), keep=3)
                    hits.append(SecretHit(path=rel, line=i, kind=kind, snippet=snippet))

            for rule, rx in _INSECURE_REGEXES:
                m = rx.search(sline)
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
            max_execution_time=300,
            respect_context_window=True,
        )
    except Exception:
        return None

def _crewai_out_to_text(out) -> str:
    """
    CrewAI may return a string, or an object with one of several fields.
    This grabs the first non-empty string we can find.
    """
    if out is None:
        return ""
    # direct string
    if isinstance(out, str):
        return out.strip()

    # common attributes seen across versions
    for attr in ("output", "final_output", "raw_output", "result", "value", "content", "text"):
        val = getattr(out, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()

    # dict-like
    if isinstance(out, dict):
        for k in ("output", "final_output", "raw_output", "result", "value", "content", "text"):
            v = out.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()

    # pydantic / custom objects: scan __dict__ for first string field
    d = getattr(out, "__dict__", None)
    if isinstance(d, dict):
        for v in d.values():
            if isinstance(v, str) and v.strip():
                return v.strip()

    # fallback to repr
    return (repr(out) or "").strip()

def _llm_recommendations_with(
    advisor,
    vulns: List[Vuln],
    secrets: List[SecretHit],
    issues: List[Issue],
    verbose: bool = False
) -> str:
    # If nothing was found, return short hygiene text.
    if not (vulns or secrets or issues):
        return (
            "No dependency vulnerabilities, secrets, or risky patterns were detected in this scan.\n\n"
            "**Keep in mind:** absence of findings ≠ absolute security. Threats evolve and coverage is imperfect.\n"
            "- Re-scan regularly (after dependency updates and before releases).\n"
            "- Enable automated dependency updates and lockfile hygiene.\n"
            "- Consider a CI policy to block new secrets or High/Critical CVEs.\n"
        )

    if advisor is None:
        if verbose:
            _log(True, "  ! LLM advisor is None (crewai import/Agent creation failed).")
        return ""

    try:
        from crewai import Task

        # Build compact metrics (no raw per-file secrets/patterns here).
        sev_counts = Counter(v.severity for v in vulns)
        eco_counts = Counter((v.ecosystem or "?") for v in vulns)
        top_pkgs = Counter((v.ecosystem or "?", v.package or "?", v.version or "?") for v in vulns).most_common(10)

        metrics = {
            "total_vulns": len(vulns),
            "severity_counts": dict(sev_counts),
            "ecosystem_counts": dict(eco_counts),
            "top_packages": [
                {"ecosystem": e, "package": p, "version": ver, "advisories": n}
                for ((e, p, ver), n) in top_pkgs
            ],
        }
        # Representative advisory summaries (trimmed, de-duped) to improve the narrative
        seen = set()
        samples: List[str] = []
        for v in sorted(vulns, key=_sev_sort_key):
            s = (v.summary or "").strip()
            if not s:
                continue
            s_norm = s.lower()
            if s_norm in seen:
                continue
            samples.append(s[:180])
            seen.add(s_norm)
            if len(samples) >= 20:
                break


        prompt = (
            "You are a security advisor.\n"
            "Using the metrics and sample advisory summaries below, produce:\n"
            "A) One tight paragraph (<=140 words) summarizing the vulnerability landscape, highlighting dominant risk categories.\n"
            "B) Five prioritized, concrete remediation recommendations as concise bullets (no vendor/tool names).\n"
            "Keep it technical and non-boilerplate.\n\n"
            f"Metrics JSON:\n{json.dumps(metrics, indent=2)}\n\n"
            "Sample advisory summaries (truncated):\n- " + "\n- ".join(samples)
        )



        # Build the task; some CrewAI versions accept 'agent=advisor'.
        try:
            task = Task(description=prompt, expected_output="Markdown with the specified sections.", agent=advisor)
        except TypeError:
            # Older signatures don't accept 'agent'; that's fine.
            task = Task(description=prompt, expected_output="Markdown with the specified sections.")

        _log(verbose, "  … calling ward advisor")
        out = advisor.execute_task(task)
        _log(verbose, "  … ward advisor returned")

        text = _crewai_out_to_text(out)
        if verbose:
            _log(True, f"  … LLM returned type={type(out).__name__}, extracted chars={len(text)}")

        return text
    except Exception as e:
        if verbose:
            _log(True, f"  ! LLM error: {e.__class__.__name__}: {e}")
        return ""

_THEMES: List[Tuple[str, re.Pattern]] = [
    ("SSRF", re.compile(r"\bserver\s*side\s*request\s*forgery|\bssrf\b", re.I)),
    ("Path/Dir Traversal", re.compile(r"\bpath\s*traversal|\bdirectory\s*traversal|\b\.\./", re.I)),
    ("LFI/RFI", re.compile(r"\blocal\s*file\s*inclusion|\brfi\b|\blfi\b", re.I)),
    ("Open Redirect", re.compile(r"\bopen\s*redirect\b", re.I)),
    ("CORS Misconfig", re.compile(r"\bcors\b|\borigin\b", re.I)),
    ("DoS", re.compile(r"\bdenial\s*of\s*service|\bdos\b", re.I)),
    ("AuthN/AuthZ", re.compile(r"\bauthenticat|\bauthoriz|\bprivilege\b|\bcsrf\b", re.I)),
    ("Info Disclosure", re.compile(r"\binformation\s*disclosure|\bexpos", re.I)),
    ("Insecure Transport", re.compile(r"\bhttp://|\binsecure\s+communication|\bssl\b|\btls\b", re.I)),
    ("Arbitrary File Access", re.compile(r"\barbitrary\s+file|\bfile\s+read|\bfile\s+write", re.I)),
]

def _extract_theme_counts(vulns: List[Vuln]) -> List[Tuple[str, int]]:
    cnt = Counter()
    for v in vulns:
        s = (v.summary or "").lower()
        for label, rx in _THEMES:
            if rx.search(s):
                cnt[label] += 1
    return cnt.most_common(8)


def _build_vulnerability_summary_md(vulns: List[Vuln]) -> str:
    if not vulns:
        return "_No dependency vulnerabilities detected._\n"

    by_sev = Counter(v.severity for v in vulns)
    by_eco = Counter((v.ecosystem or "?") for v in vulns)
    by_pkg = Counter((v.ecosystem or "?", v.package or "?", v.version or "?") for v in vulns)

    lines: List[str] = []
    lines.append("### Overview")
    lines.append(
        "- By severity: " +
        ", ".join(f"{k}:{by_sev.get(k,0)}" for k in ("CRITICAL","HIGH","MEDIUM","LOW","UNKNOWN"))
    )
    lines.append(
        "- By ecosystem: " +
        (", ".join(f"{eco}:{cnt}" for eco, cnt in by_eco.most_common()) or "–")
    )
    lines.append("")
    lines.append("### Most-affected packages")
    for (eco, pkg, ver), cnt in by_pkg.most_common(10):
        lines.append(f"- `{eco}:{pkg}@{ver}` — {cnt} advisory(ies)")
    lines.append("")
    
    themes = _extract_theme_counts(vulns)
    if themes:
        lines.append("")
        lines.append("### Impact Themes")
        for label, n in themes:
            lines.append(f"- {label}: {n}")
        lines.append("")

    return "\n".join(lines)



# ---------- summarize & render ----------
def _sev_counts(vulns: List[Vuln]) -> Dict[str, int]:
    c = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}
    for v in vulns:
        c[v.severity] = c.get(v.severity, 0) + 1
    return c

def _vuln_details_url(vuln_id: str) -> str:
    if not vuln_id:
        return ""
    if vuln_id.startswith("GHSA-"):
        return f"https://github.com/advisories/{vuln_id}"
    return f"https://osv.dev/vulnerability/{vuln_id}"

def render_ward_markdown(
    title: str,
    root: Path,
    vulns: List[Vuln],
    secrets: List[SecretHit],
    issues: List[Issue],
    recommendations_md: str = "",
    method_tag: str = "",
) -> str:
    sev = _sev_counts(vulns)
    lines: List[str] = []
     # --- header -------
    lines.append(f"# {title}\n")
    lines.append(f"_Root_: `{root.name}`  ")
    if method_tag:
        lines.append(f"_Scan method_: **{method_tag}**  ")
    lines.append(
        "Findings: **{t} vulns** (C:{c} H:{h} M:{m} L:{l} U:{u}), **{S} secrets**, **{I} risky patterns**\n"
        .format(
            t=len(vulns), c=sev["CRITICAL"], h=sev["HIGH"], m=sev["MEDIUM"], l=sev["LOW"], u=sev["UNKNOWN"],
            S=len(secrets), I=len(issues)
        )
    )
    # Summary (code-built, not LLM)
    lines.append("## Summary")
    lines.append(_build_vulnerability_summary_md(vulns))
    if vulns:
        buckets = defaultdict(list)  # key: (ecosystem, package, version)
        for v in vulns:
            key = (v.ecosystem or "?", v.package or "?", v.version or "?")
            buckets[key].append(v)

        lines.append("## Vulnerabilities by Package")
        # sort: most advisories first, then ecosystem/package for stable order
        for (eco, pkg, ver), items in sorted(
            buckets.items(),
            key=lambda kv: (-len(kv[1]), kv[0][0], kv[0][1])
        ):
            ids = sorted({x.id for x in items if x.id})
            preview = ", ".join(ids[:8])
            more = f" …(+{len(ids)-8} more IDs)" if len(ids) > 8 else ""
            lines.append(f"- `{eco}:{pkg}@{ver}` — **{len(items)} advisories** (e.g., {preview}{more})")
        lines.append("")

    if vulns:
        lines.append("## Top Vulnerabilities")
        for v in sorted(vulns, key=_sev_sort_key)[:12]:
            pkg = f"{v.ecosystem}:{v.package}@{v.version}" if v.package else v.ecosystem
            desc = (v.summary or "").strip() or "(see details)"
            url = _vuln_details_url(v.id)
            lines.append(f"- `{v.id}` **{v.severity}** — {pkg} — [details]({url}) — {desc}")
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
        lines.append("## LLM Summary & Recommendations\n")
        lines.append((recommendations_md or "_(LLM returned no text.)_").strip())
        lines.append("")

    # Monitoring & Continuous Scans — concise, actionable, generic
    lines.append("## Monitoring & Continuous Scans")
    lines.append("- **Baseline diff in CI:** Keep `security/osv-baseline.json`. Fail PRs only on **NEW** `HIGH`/`CRITICAL` advisories vs baseline.")
    lines.append("- **Create/refresh baseline:** run `poltergeist ward -p <root> --json` and copy the JSON to `security/osv-baseline.json` after triage.")
    lines.append("- **How to diff IDs (example):** extract IDs and compare to fail on new ones.")
    lines.extend([
        "```sh",
        "# assumes: previous baseline at security/osv-baseline.json",
        "# and current scan saved as ward_current.json",
        "jq -r '.vulns[].id' security/osv-baseline.json | sort > base.txt",
        "jq -r '.vulns[].id' ward_current.json           | sort > curr.txt",
        'NEW=$(comm -13 base.txt curr.txt)',
        'test -z \"$NEW\" || { echo \"New advisories:\"; echo \"$NEW\"; exit 1; }',
        "```",
    ])
    lines.append("- **Scheduled sweep:** run a weekly full scan on the default branch and auto-open an issue for any net-new `MEDIUM+`.")
    lines.append("- **Release gate:** block releases unless `HIGH+` are 0 or time-boxed with owner + expiry in an allowlist.")
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
    llm: bool = True,        # LLM ON by default
    write_json: bool = False # JSON is OFF by default; enable with --json
) -> Path:
    root = Path(path).resolve()
    _log(verbose, f"▶ Ward scanning root: {root}")

    # 0) Discover files for text scanning
    files = walk_files(root, include or [], exclude or [], [e.lower() for e in (exts or [])] or None, max_files)
    _log(verbose, f"• Files discovered: {len(files)}")

    # 1) Dependency vulnerabilities (OSV CLI or API fallback)
    method_tag = "OSV disabled"
    vulns: List[Vuln] = []
    if use_osv:
        exe = _which("osv-scanner")
        if exe:
            method_tag = "OSV CLI"
            _log(verbose, "• Running OSV-Scanner…")
            vulns = _osv_scan(root, verbose=verbose)
            sev = _sev_counts(vulns)
            _log(verbose, f"  ← OSV (CLI) complete: {len(vulns)} vulns (C:{sev['CRITICAL']} H:{sev['HIGH']} M:{sev['MEDIUM']} L:{sev['LOW']} U:{sev['UNKNOWN']})")
        else:
            method_tag = "OSV API"
            _log(verbose, "• OSV-Scanner not found on PATH — running manual OSV API scan instead.")
            _log(verbose, "  tip: install OSV-Scanner for deeper coverage (https://github.com/google/osv-scanner)")
            vulns = _osv_api_scan(root, verbose=verbose)
            sev = _sev_counts(vulns)
            _log(verbose, f"  ← OSV (API) complete: {len(vulns)} vulns (C:{sev['CRITICAL']} H:{sev['HIGH']} M:{sev['MEDIUM']} L:{sev['LOW']} U:{sev['UNKNOWN']})")
    else:
        _log(verbose, "• Skipping OSV-Scanner (disabled)")

    # 1b) Enrich severity/summary from OSV per-ID endpoint (fills UNKNOWN / empty)
    if vulns:
        _log(verbose, "• Enriching vulnerability details from OSV…")
        _enrich_vulns_with_details(vulns, verbose=verbose)
        sev = _sev_counts(vulns)
        _log(verbose, f"  ← Enrichment complete (C:{sev['CRITICAL']} H:{sev['HIGH']} M:{sev['MEDIUM']} L:{sev['LOW']} U:{sev['UNKNOWN']})")


    # 2) Secrets + risky patterns
    _log(verbose, "• Scanning for secrets and risky patterns…")
    secrets, issues = scan_secrets_and_issues(files, root, redact=redact, preview=preview, risky_context=80)
    _log(verbose, f"  ← Text scan complete: {len(secrets)} secrets, {len(issues)} risky patterns")

    # 3) LLM recommendations
    recommendations_md = ""
    if llm:
        _log(verbose, "• Generating LLM recommendations…")
        try:
            from geist_agent.utils import EnvUtils
            if hasattr(EnvUtils, "load_env_for_tool"):
                EnvUtils.load_env_for_tool()
        except Exception:
            pass
        with _llm_profile("WARD"):  
            advisor = _get_ward_advisor()
            if advisor is None:
                _log(verbose, "  ! ward advisor not available; skipping LLM recommendations.")
            else:
                recommendations_md = _llm_recommendations_with(advisor, vulns, secrets, issues, verbose=verbose)
        _log(verbose, f"  ← LLM step complete (chars: {len(recommendations_md)})")
    else:
        _log(verbose, "• Skipping LLM recommendations (disabled)")

    # 4) Render & write (topic = repo root)
    _log(verbose, "• Rendering Ward report…")
    from geist_agent.utils import ReportUtils  # local import to avoid cycles
    out_dir = PathUtils.ensure_reports_dir("ward_reports")
    topic = root.name or "unknown_root"
    md_name = ReportUtils.generate_filename(topic)  
    out_md = out_dir / md_name

    md = render_ward_markdown(
        "Ward: Security Audit",
        root,
        vulns,
        secrets,
        issues,
        recommendations_md=recommendations_md,  # includes the LLM output
        method_tag=method_tag                    # shows OSV CLI vs API in header
    )
    out_md.write_text(md, encoding="utf-8")
    _log(verbose, f"✓ Markdown written: {out_md}")


    if write_json:
        out_json = save_ward_json(root, vulns, secrets, issues)
        _log(verbose, f"✓ JSON written:     {out_json}")
    else:
        _log(verbose, "• Skipping JSON output (Add --json to generate)")

    _log(verbose, "• Done.")
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

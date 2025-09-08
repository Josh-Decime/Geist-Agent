# src/geist_agent/ward/scanning.py
from __future__ import annotations
import os, re, json, time, urllib.request, urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import Counter
from geist_agent.ward.ward_common import (
    _log, _which, _run, _best_severity_from_osv_payload, _max_sev_from_list,
    _llm_profile, SCAN_META, Vuln, SecretHit, Issue
)

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
        if any(seg in base for seg in ("/node_modules","/.git","/.venv","/venv","/dist","/build",
                                       "/.egg-info","/target","/out","/.mypy_cache","/.pytest_cache")):
            continue
        for f in files:
            if f in names:
                found.append(str(Path(cur, f)))
    return found

# === OSV API FALLBACK: dependency collectors ===
def _pep_dep_exact(spec: str) -> Tuple[str, Optional[str]]:
    s = (spec or "").strip().split(";", 1)[0]
    name = s.split("[", 1)[0].strip()
    if "==" in s:
        return name, s.split("==", 1)[1].strip()
    return name, None

def _poetry_exact_version(spec) -> Optional[str]:
    if isinstance(spec, str):
        return spec if spec[:1].isdigit() else None
    if isinstance(spec, dict):
        v = spec.get("version")
        return v if isinstance(v, str) and v[:1].isdigit() else None
    return None

def _npm_semver_exact(spec: str) -> Optional[str]:
    s = (spec or "").strip()
    s = s.lstrip("^~").split("||")[0].strip()
    return s if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+].+)?", s) else None

def _collect_pinned_deps_for_osv(root: Path) -> List[Dict[str, str]]:
    deps: List[Dict[str, str]] = []
    # Python: requirements*.txt
    for req in root.rglob("requirements*.txt"):
        try:
            for line in req.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if "==" in s:
                    name, ver = s.split("==", 1)
                    name = name.split("[", 1)[0].strip()
                    ver = ver.strip()
                    if name and ver:
                        deps.append({"ecosystem": "PyPI", "name": name, "version": ver})
        except Exception:
            pass
    # Python: pyproject.toml (tomllib if present)
    try:
        import tomllib  # type: ignore
        for pp in root.rglob("pyproject.toml"):
            try:
                data = tomllib.loads(pp.read_text(encoding="utf-8"))
            except Exception:
                continue
            for spec in (data.get("project", {}).get("dependencies") or []):
                name, ver = _pep_dep_exact(spec)
                if name and ver:
                    deps.append({"ecosystem": "PyPI", "name": name, "version": ver})
            for _g, items in (data.get("project", {}).get("optional-dependencies") or {}).items():
                for spec in items or []:
                    name, ver = _pep_dep_exact(spec)
                    if name and ver:
                        deps.append({"ecosystem": "PyPI", "name": name, "version": ver})
            poetry = data.get("tool", {}).get("poetry", {})
            for name, spec in (poetry.get("dependencies") or {}).items():
                if name.lower() == "python":
                    continue
                ver = _poetry_exact_version(spec)
                if name and ver:
                    deps.append({"ecosystem": "PyPI", "name": name, "version": ver})
    except ModuleNotFoundError:
        pass
    # Node: package-lock.json (npm v7+)
    for lock in root.rglob("package-lock.json"):
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
            pkgs = data.get("packages") or {}
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
    # Node: package.json (pinned only)
    for pj in root.rglob("package.json"):
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
            for section in ("dependencies","devDependencies","peerDependencies","optionalDependencies"):
                m = data.get(section) or {}
                for name, spec in m.items():
                    ver = _npm_semver_exact(spec)
                    if name and ver:
                        deps.append({"ecosystem": "npm", "name": name, "version": ver})
        except Exception:
            pass
    # de-dupe
    seen = set(); uniq: List[Dict[str, str]] = []
    for d in deps:
        key = (d["ecosystem"], d["name"], d["version"])
        if key not in seen:
            seen.add(key); uniq.append(d)
    return uniq

# ---------- scanners: dependency vulns via OSV-Scanner (optional) ----------
def _osv_scan(root: Path, verbose: bool = False) -> List[Vuln]:
    exe = _which("osv-scanner")
    if not exe:
        return []
    manifests = _collect_manifests(root)
    SCAN_META["manifests"] = len(manifests)
    SCAN_META["manifest_paths"] = manifests[:3]
    _lockfile_names = {
        "package-lock.json","pnpm-lock.yaml","yarn.lock","poetry.lock","Pipfile.lock","go.sum",
        "Cargo.lock","Gemfile.lock","composer.lock","packages.lock.json",
    }
    lockfile_paths = [p for p in manifests if Path(p).name in _lockfile_names]
    SCAN_META["source"] = "CLI"
    SCAN_META["lockfiles"] = len(lockfile_paths)
    SCAN_META["lockfile_paths"] = lockfile_paths[:3]
    cmd = [exe, "--format=json", "--skip-git", *sum([["-L", m] for m in manifests], [])] if manifests \
          else [exe, "--format=json", "--skip-git", f"dir:{root}"]
    code, out, err = _run(cmd)
    if code != 0 or not (out.strip() or err.strip()):
        return []
    try:
        data = json.loads(out.strip() or err.strip())
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
                    id=v.get("id", "") or "", ecosystem=eco, package=name, version=vers or "",
                    severity=sev, summary=summary or ""
                ))
    return vulns

# === OSV API FALLBACK: API scanner ===
def _osv_api_scan(root: Path, verbose: bool = True) -> List[Vuln]:
    _log(verbose, "• Collecting pinned dependencies for OSV API…")
    deps = _collect_pinned_deps_for_osv(root)
    SCAN_META["source"] = "API"
    SCAN_META["pinned_deps"] = len(deps)
    SCAN_META["api_queries"] = len(deps)
    if not deps:
        _log(verbose, "  ← No pinned deps found to query; skipping OSV API.")
        return []
    HARD_CAP   = int(os.getenv("WARD_OSV_MAX_QUERIES", "0"))
    BATCH      = int(os.getenv("WARD_OSV_BATCH_SIZE", "200"))
    MIN_BATCH  = int(os.getenv("WARD_OSV_MIN_BATCH_SIZE", "25"))
    TIMEOUT_S  = int(os.getenv("WARD_OSV_HTTP_TIMEOUT", "60"))
    MAX_RETRY  = int(os.getenv("WARD_OSV_MAX_RETRY", "5"))
    BO_START   = float(os.getenv("WARD_OSV_BACKOFF_START", "1.0"))
    BO_MAX     = float(os.getenv("WARD_OSV_BACKOFF_MAX", "30.0"))

    uniq: List[Dict[str, str]] = []
    seen = set()
    for d in deps:
        key = (d["ecosystem"], d["name"], d["version"])
        if key not in seen:
            seen.add(key); uniq.append(d)
    if HARD_CAP > 0 and len(uniq) > HARD_CAP:
        _log(verbose, f"• OSV API: capping queries {len(uniq)} → {HARD_CAP} (WARD_OSV_MAX_QUERIES)")
        uniq = uniq[:HARD_CAP]

    total = len(uniq)
    _log(verbose, f"• Contacting OSV API with {total} queries (batch start={BATCH})…")
    all_queries = [{"package": {"name": d["name"], "ecosystem": d["ecosystem"]}, "version": d["version"]} for d in uniq]
    findings: List[Vuln] = []; i = 0; curr_batch = max(BATCH, MIN_BATCH)

    while i < total:
        end = min(i + curr_batch, total)
        chunk = all_queries[i:end]
        dep_slice = uniq[i:end]
        retries = 0; backoff = BO_START
        while True:
            payload = {"queries": chunk}
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url="https://api.osv.dev/v1/querybatch",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    obj = json.loads(raw)
                for d, res in zip(dep_slice, obj.get("results", [])):
                    for v in res.get("vulns") or []:
                        findings.append(Vuln(
                            id=v.get("id", "") or "",
                            ecosystem=d["ecosystem"],
                            package=d["name"],
                            version=d["version"],
                            severity=_max_sev_from_list(v.get("severity", []) or []),
                            summary=(v.get("summary") or v.get("details", "")[:140]) or "",
                        ))
                i = end
                time.sleep(0.05)
                break
            except urllib.error.HTTPError as e:
                code = getattr(e, "code", None)
                if code in (429, 500, 502, 503, 504) and retries < MAX_RETRY:
                    time.sleep(backoff); backoff = min(backoff * 2.0, BO_MAX); retries += 1; continue
                if code == 400 and len(chunk) > MIN_BATCH:
                    new_size = max(MIN_BATCH, len(chunk) // 2)
                    curr_batch = new_size
                    end = min(i + curr_batch, total)
                    chunk = all_queries[i:end]
                    dep_slice = uniq[i:end]
                    retries = 0; backoff = BO_START
                    continue
                i = end
                break
            except urllib.error.URLError as e:
                if retries < MAX_RETRY:
                    time.sleep(backoff); backoff = min(backoff * 2.0, BO_MAX); retries += 1; continue
                i = end
                break
    _log(verbose, f"  ← OSV API complete: {len(findings)} vulns")
    return findings

def _enrich_vulns_with_details(vulns: List[Vuln], *, limit: Optional[int] = None, verbose: bool = False) -> None:
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
            time.sleep(0.05)
        except Exception:
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
_SECRET_REGEXES = [(k, re.compile(rx)) for k, rx in _SECRET_PATTERNS]
_INSECURE_REGEXES = [(k, re.compile(rx)) for k, rx in _INSECURE_PATTERNS]

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
    hits: List[SecretHit] = []; issues: List[Issue] = []
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
                    start = max(0, m.start() - 8); end = min(len(sline), m.end() + 8)
                    issues.append(Issue(path=rel, line=i, rule=rule, snippet=sline[start:end][:risky_context]))
    return hits, issues


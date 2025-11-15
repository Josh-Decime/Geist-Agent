# src/geist_agent/ward/reporting.py
from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict, Counter
import re, json

from geist_agent.ward.ward_common import Vuln, SecretHit, Issue, _sev_sort_key
from geist_agent.utils import PathUtils

# ---------- LLM advisor ----------
def _get_ward_advisor():
    try:
        from crewai import Agent
        return Agent(
            role="Security Advisor",
            goal=("Given dependency CVEs, secrets, and risky patterns, produce prioritized remediation steps "
                  "with concise code/config examples."),
            backstory="Pragmatic AppSec engineer focused on high-signal fixes.",
            verbose=False, max_iter=1, cache=True, max_execution_time=300, respect_context_window=True,
        )
    except Exception:
        return None

def _crewai_out_to_text(out) -> str:
    if out is None:
        return ""
    if isinstance(out, str):
        return out.strip()
    for attr in ("output","final_output","raw_output","result","value","content","text"):
        val = getattr(out, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    if isinstance(out, dict):
        for k in ("output","final_output","raw_output","result","value","content","text"):
            v = out.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    d = getattr(out, "__dict__", None)
    if isinstance(d, dict):
        for v in d.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
    return (repr(out) or "").strip()

def llm_recommendations_with(advisor, vulns: List[Vuln], secrets: List[SecretHit], issues: List[Issue], verbose: bool=False) -> str:
    if not (vulns or secrets or issues):
        return (
            "No dependency vulnerabilities, secrets, or risky patterns were detected in this scan.\n\n"
            "**Keep in mind:** absence of findings ≠ absolute security. Threats evolve and coverage is imperfect.\n"
            "- Re-scan regularly (after dependency updates and before releases).\n"
            "- Enable automated dependency updates and lockfile hygiene.\n"
            "- Consider a CI policy to block new secrets or High/Critical CVEs.\n"
        )
    if advisor is None:
        return ""
    try:
        from crewai import Task
        sev_counts = Counter(v.severity for v in vulns)
        eco_counts = Counter((v.ecosystem or "?") for v in vulns)
        top_pkgs = Counter((v.ecosystem or "?", v.package or "?", v.version or "?") for v in vulns).most_common(10)
        metrics = {
            "total_vulns": len(vulns),
            "severity_counts": dict(sev_counts),
            "ecosystem_counts": dict(eco_counts),
            "top_packages": [{"ecosystem": e, "package": p, "version": ver, "advisories": n} for ((e,p,ver),n) in top_pkgs],
        }
        seen = set(); samples: List[str] = []
        for v in sorted(vulns, key=_sev_sort_key):
            s = (v.summary or "").strip()
            if not s: continue
            s_norm = s.lower()
            if s_norm in seen: continue
            samples.append(s[:180]); seen.add(s_norm)
            if len(samples) >= 20: break
        prompt = (
            "You are a security advisor.\n"
            "Using the metrics and sample advisory summaries below, produce:\n"
            "A) One tight paragraph (<=140 words) summarizing the vulnerability landscape.\n"
            "B) Five prioritized, concrete remediation recommendations as concise bullets (no vendor names).\n\n"
            f"Metrics JSON:\n{json.dumps(metrics, indent=2)}\n\n"
            "Sample advisory summaries (truncated):\n- " + "\n- ".join(samples)
        )
        try:
            task = Task(description=prompt, expected_output="Markdown with the specified sections.", agent=advisor)
        except TypeError:
            task = Task(description=prompt, expected_output="Markdown with the specified sections.")
        out = advisor.execute_task(task)
        return _crewai_out_to_text(out)
    except Exception:
        return ""

# ---------- themes & summaries ----------
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
        ", ".join(f"{k}:{by_sev.get(k, 0)}" for k in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"))
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

def _format_scan_input(meta: Dict[str, Any]) -> str:
    from pathlib import Path as _Path
    src = meta.get("source") or "?"
    if src == "CLI":
        k = meta.get("lockfiles", 0); m = meta.get("manifests", 0)
        lf_names = [_Path(p).name for p in (meta.get("lockfile_paths") or [])]
        mf_names = [_Path(p).name for p in (meta.get("manifest_paths") or [])]
        lf_tail = f" ({', '.join(lf_names)}{('…' if k > len(lf_names) else '')})" if lf_names else ""
        mf_tail = f" ({', '.join(mf_names)}{('…' if m > len(mf_names) else '')})" if mf_names else ""
        return f"_Input_: **OSV CLI** — lockfiles: **{k}**{lf_tail}; manifests: **{m}**{mf_tail}  "
    elif src == "API":
        q = meta.get("api_queries", 0)
        return f"_Input_: **OSV API** — pinned dependency queries: **{q}**  "
    return "_Input_: (unknown)  "

def render_ward_markdown(
    title: str,
    root: Path,
    vulns: List[Vuln],
    secrets: List[SecretHit],
    issues: List[Issue],
    recommendations_md: str = "",
    method_tag: str = "",
    scan_meta: Optional[Dict[str, Any]] = None,
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
    if scan_meta:
        lines.append(_format_scan_input(scan_meta))
        lines.append("")

    # Summary (code-built, not LLM)
    lines.append("## Summary")
    lines.append(_build_vulnerability_summary_md(vulns))
    if vulns:
        buckets = defaultdict(list)
        for v in vulns:
            key = (v.ecosystem or "?", v.package or "?", v.version or "?")
            buckets[key].append(v)
        lines.append("## Vulnerabilities by Package")
        for (eco, pkg, ver), items in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0][0], kv[0][1])):
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
    out_json = out_dir / f"ward_{int(__import__('time').time())}.json"
    payload = {
        "root": str(root),
        "vulns": [v.__dict__ for v in vulns],
        "secrets": [s.__dict__ for s in secrets],
        "issues": [r.__dict__ for r in issues],
        "generated_at": int(__import__('time').time()),
    }
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_json


# src/geist_agent/ward/runner.py
from __future__ import annotations
import argparse
from pathlib import Path
from typing import List, Optional
from geist_agent.utils import walk_files_compat as walk_files, PathUtils, ReportUtils

# Core pieces from the split modules
from geist_agent.ward.common import _log, _reset_scan_meta, _llm_profile, SCAN_META
from geist_agent.ward.scanning import (
    _osv_scan, _osv_api_scan, _enrich_vulns_with_details, scan_secrets_and_issues
)
from geist_agent.ward.reporting import (
    _sev_counts, render_ward_markdown, save_ward_json, _format_scan_input,
    _vuln_details_url, _build_vulnerability_summary_md, _extract_theme_counts,
    _get_ward_advisor, llm_recommendations_with,
)

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
    llm: bool = True,
    write_json: bool = False,
    force_api: bool = False,
) -> Path:
    root = Path(path).resolve()
    _reset_scan_meta()
    _log(verbose, f"▶ Ward scanning root: {root}")

    # 0) Discover files for text scanning
    files = walk_files(root, include or [], exclude or [], [e.lower() for e in (exts or [])] or None, max_files)
    _log(verbose, f"• Files discovered: {len(files)}")

    # 1) Dependency vulnerabilities (OSV CLI or API)
    method_tag = "OSV disabled"
    vulns = []
    if use_osv:
        from geist_agent.ward.common import _which
        exe = _which("osv-scanner")
        if exe and not force_api:
            method_tag = "OSV CLI"
            _log(verbose, "• Running OSV-Scanner…")
            vulns = _osv_scan(root, verbose=verbose)
            sev = _sev_counts(vulns)
            _log(verbose, f"  ← OSV (CLI) complete: {len(vulns)} vulns (C:{sev['CRITICAL']} H:{sev['HIGH']} M:{sev['MEDIUM']} L:{sev['LOW']} U:{sev['UNKNOWN']})")
            _log(verbose, "  " + _format_scan_input(SCAN_META))
        else:
            method_tag = "OSV API"
            if exe and force_api:
                _log(verbose, "• OSV CLI present but disabled — using OSV API scan (forced).")
            else:
                _log(verbose, "• OSV-Scanner not found on PATH — running manual OSV API scan instead.")
                _log(verbose, "  tip: install OSV-Scanner for deeper coverage (https://github.com/google/osv-scanner)")
            vulns = _osv_api_scan(root, verbose=verbose)
            sev = _sev_counts(vulns)
            _log(verbose, f"  ← OSV (API) complete: {len(vulns)} vulns (C:{sev['CRITICAL']} H:{sev['HIGH']} M:{sev['MEDIUM']} L:{sev['LOW']} U:{sev['UNKNOWN']})")
            _log(verbose, "  " + _format_scan_input(SCAN_META))

        # Auto-fallback when CLI had nothing to scan
        if method_tag == "OSV CLI" and SCAN_META["lockfiles"] == 0 and SCAN_META["manifests"] == 0 and not vulns:
            _log(verbose, "• No lockfiles or manifests for OSV CLI — falling back to OSV API pinned-deps scan…")
            method_tag = "OSV API (fallback)"
            v2 = _osv_api_scan(root, verbose=verbose)
            if v2:
                vulns = v2
                sev = _sev_counts(vulns)
                _log(verbose, f"  ← OSV (API fallback) complete: {len(vulns)} vulns (C:{sev['CRITICAL']} H:{sev['HIGH']} M:{sev['MEDIUM']} L:{sev['LOW']} U:{sev['UNKNOWN']})")
            _log(verbose, "  " + _format_scan_input(SCAN_META))
    else:
        method_tag = "OSV API (forced)" if force_api else "OSV API"
        _log(verbose, "• OSV CLI disabled — using OSV API scan.")
        vulns = _osv_api_scan(root, verbose=verbose)
        sev = _sev_counts(vulns)
        _log(verbose, f"  ← OSV (API) complete: {len(vulns)} vulns (C:{sev['CRITICAL']} H:{sev['HIGH']} M:{sev['MEDIUM']} L:{sev['LOW']} U:{sev['UNKNOWN']})")
        _log(verbose, "  " + _format_scan_input(SCAN_META))

    # 1b) Enrich severity/summary
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
                recommendations_md = llm_recommendations_with(advisor, vulns, secrets, issues, verbose=verbose)
        _log(verbose, f"  ← LLM step complete (chars: {len(recommendations_md)})")
    else:
        _log(verbose, "• Skipping LLM recommendations (disabled)")

    # 4) Render & write (topic = repo root)
    _log(verbose, "• Rendering Ward report…")
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
        recommendations_md=recommendations_md,
        method_tag=method_tag,
        scan_meta=SCAN_META,
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
    ap.add_argument("--no-osv", action="store_true", help="Force OSV API scan (skip the osv-scanner CLI even if installed)")
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
        force_api=args.no_osv,
    )

if __name__ == "__main__":
    main()

# src/geist_agent/ward/common.py
from __future__ import annotations
import os, sys, json, shutil, subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from contextlib import contextmanager

# ---------- tiny logger ----------
def _log(enabled: bool, msg: str):
    if enabled:
        print(msg, file=sys.stderr, flush=True)

# --- scan metadata for the report header ---
SCAN_META: Dict[str, Any] = {
    "source": "",            # "CLI" or "API"
    "lockfiles": 0,
    "lockfile_paths": [],
    "manifests": 0,
    "manifest_paths": [],
    "api_queries": 0,
    "pinned_deps": 0,
}
def _reset_scan_meta():
    SCAN_META["source"] = ""
    SCAN_META["lockfiles"] = 0
    SCAN_META["lockfile_paths"] = []
    SCAN_META["manifests"] = 0
    SCAN_META["manifest_paths"] = []
    SCAN_META["api_queries"] = 0
    SCAN_META["pinned_deps"] = 0

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
    snippet: str

@dataclass
class Issue:
    path: str
    line: int
    rule: str
    snippet: str

# ---------- severity helpers (shared) ----------
SEV_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}
def _sev_sort_key(v: Vuln):
    return (-SEV_ORDER.get(v.severity, 0), v.ecosystem, v.package)

def _max_sev_from_list(sev_list: List[dict]) -> str:
    buckets = []
    for entry in sev_list or []:
        score = entry.get("score")
        s = None
        if isinstance(score, (int, float)):
            s = float(score)
        elif isinstance(score, str):
            try:
                s = float(score)
            except ValueError:
                s = None
        if s is not None:
            if s >= 9.0:      buckets.append("CRITICAL")
            elif s >= 7.0:    buckets.append("HIGH")
            elif s >= 4.0:    buckets.append("MEDIUM")
            elif s > 0.0:     buckets.append("LOW")
            continue
        val = json.dumps(entry).upper()
        if   "CRITICAL" in val: buckets.append("CRITICAL")
        elif "HIGH"     in val: buckets.append("HIGH")
        elif "MODERATE" in val or "MEDIUM" in val: buckets.append("MEDIUM")
        elif "LOW"      in val: buckets.append("LOW")
    if "CRITICAL" in buckets: return "CRITICAL"
    if "HIGH"     in buckets: return "HIGH"
    if "MEDIUM"   in buckets: return "MEDIUM"
    if "LOW"      in buckets: return "LOW"
    return "UNKNOWN"

def _best_severity_from_osv_payload(osv_obj: dict) -> str:
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
    for key in _LLM_KEYS:
        val = os.getenv(f"{prefix}_{key}")
        if val:
            os.environ[key] = val

@contextmanager
def _llm_profile(prefix: str):
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


# src/geist_agent/doctor.py
from __future__ import annotations
# ---------- imports ----------
import os, json, sys, urllib.request
from dataclasses import dataclass
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
from typing import Any, Callable, Dict, List

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown

from geist_agent.utils import EnvUtils, PathUtils

console = Console()

# ---------- utils ----------
def _pkg_version() -> str:
    try:
        return version("geist_agent")
    except PackageNotFoundError:
        return "0.0.0-dev"

def _ok(ok: bool) -> str:
    return "✅" if ok else "❌"

@dataclass
class CheckResult:
    name: str
    ok: bool
    info: Dict[str, Any]
    critical: bool = True

Check = Callable[[], CheckResult]

# ---------- checks ----------
def check_versions() -> CheckResult:
    info = {"geist_agent": _pkg_version(), "python": sys.version.split()[0]}
    return CheckResult("Versions", True, info, critical=False)

def check_env() -> CheckResult:
    model = os.getenv("MODEL") or ""
    base = os.getenv("API_BASE") or ""
    ok = bool(model) and bool(base)
    return CheckResult("Environment", ok, {"MODEL": model or "<unset>", "API_BASE": base or "<unset>"})

def check_ollama() -> CheckResult:
    base = os.getenv("API_BASE") or "http://localhost:11434"
    model = os.getenv("MODEL") or ""
    want = (model.split("/", 1)[-1] if "/" in model else model)
    info: Dict[str, Any] = {"API_BASE": base, "MODEL": model, "present": False, "installed": []}
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        names = [m["name"] for m in data.get("models", [])]
        info["installed"] = names
        info["present"] = (want and any(n.startswith(want) for n in names))
        ok = bool(names) and info["present"]
        return CheckResult("Ollama", ok, info)
    except Exception as e:
        info["error"] = str(e)
        return CheckResult("Ollama", False, info)

def check_reports_write() -> CheckResult:
    dir_ = PathUtils.ensure_reports_dir("scrying_reports")
    test = dir_ / ".poltergeist_write_test.tmp"
    info: Dict[str, Any] = {"path": str(dir_)}
    try:
        test.write_text("ok", encoding="utf-8")
        val = test.read_text(encoding="utf-8")
        test.unlink(missing_ok=True)
        ok = (val == "ok")
        return CheckResult("Reports Write", ok, info)
    except Exception as e:
        info["error"] = str(e)
        return CheckResult("Reports Write", False, info)

CHECKS: List[Check] = [
    check_versions,
    check_env,
    check_ollama,
    check_reports_write,
]

# ---------- rendering ----------
def _render_summary(results: List[CheckResult]) -> None:
    ok_count = sum(1 for r in results if bool(r.ok))
    all_count = len(results)
    critical_fail = any((not bool(r.ok)) and r.critical for r in results)
    title = Text(f"Poltergeist Doctor — {_pkg_version()}")
    title.stylize("bold cyan")
    subtitle = Text(f"{ok_count}/{all_count} checks passed • {'All good' if not critical_fail and ok_count==all_count else 'Issues found'}")
    subtitle.stylize("green" if ok_count == all_count else "yellow")
    console.print(Panel.fit(Markdown(f"**{title}**\n\n{subtitle}"), border_style="cyan"))

def _render_table(results: List[CheckResult]) -> None:
    table = Table(title="Diagnostics", expand=True)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Details")
    for r in results:
        status = _ok(r.ok)
        if not r.critical and not r.ok:
            status += " (non-critical)"
        if r.name == "Environment":
            detail = f"MODEL={r.info.get('MODEL')}, API_BASE={r.info.get('API_BASE')}"
        elif r.name == "Ollama":
            detail = f"present={r.info.get('present')}, models={len(r.info.get('installed', []))}"
            if "error" in r.info:
                detail += f", error={r.info['error']}"
        elif r.name == "Reports Write":
            detail = f"path={r.info.get('path', '')}"
            if "error" in r.info:
                detail += f", error={r.info['error']}"
        else:
            detail = ", ".join(f"{k}={v}" for k, v in r.info.items())
        table.add_row(r.name, status, detail)
    console.print(table)

# ---------- command entry ----------
def run(as_json: bool = False) -> int:
    EnvUtils.load_env_for_tool()
    results = [chk() for chk in CHECKS]
    critical_fail = any((not r.ok) and r.critical for r in results)

    if as_json:
        payload = {
            "package_version": _pkg_version(),
            "results": [r.__dict__ for r in results],
            "ok": not critical_fail,
        }
        print(json.dumps(payload, indent=2))
        return 1 if critical_fail else 0

    _render_summary(results)
    _render_table(results)
    console.print("\n[bold green]System ready.[/bold green]" if not critical_fail else "\n[bold red]Some critical checks failed.[/bold red]")
    return 1 if critical_fail else 0

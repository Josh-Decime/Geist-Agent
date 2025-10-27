# src/geist_agent/utils.py
import re
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Iterable
from dotenv import load_dotenv
from fnmatch import fnmatch
from itertools import islice


class ReportUtils:
    """Utility class for report generation functions"""
    
    @staticmethod
    def generate_filename(topic: Optional[str] = None, max_topic_length: int = 25) -> str:
        """
        Generate a filename based on topic and timestamp
        
        Args:
            topic: The topic/subject for the report
            max_topic_length: Maximum characters for topic (default 25)
            
        Returns:
            str: Formatted filename like "Topic_Name_08-15-2025_18-32.md"
        """
        try:
            # Handle missing or invalid topic
            if not topic or not isinstance(topic, str):
                safe_topic = "unknown_topic"
            else:
                # Clean topic for filename (remove special chars, limit length)
                safe_topic = re.sub(r'[^\w\s-]', '', topic)
                safe_topic = re.sub(r'[-\s]+', '_', safe_topic)
                safe_topic = safe_topic.strip('_')[:max_topic_length]
                
                # Fallback if topic becomes empty after cleaning
                if not safe_topic:
                    safe_topic = "unknown_topic"
            
            # Generate timestamp - fallback if datetime fails
            try:
                timestamp = datetime.now().strftime("%m/%d/%Y_%H:%M")
                # Replace slashes and colons for Windows filename compatibility
                safe_timestamp = timestamp.replace('/', '-').replace(':', '-')
            except Exception:
                # Fallback timestamp if datetime fails
                safe_timestamp = "unknown_date_00-00"
            
            return f'{safe_topic}_{safe_timestamp}.md'
            
        except Exception:
            # Ultimate fallback if everything fails
            return "report_unknown.md"
        
class EnvUtils:
    @staticmethod
    def user_env_dir() -> Path:
        """Return the ~/.geist directory (created if missing)."""
        d = Path.home() / ".geist"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def user_env_path() -> Path:
        """Return the canonical user-level env location: ~/.geist/.env"""
        return EnvUtils.user_env_dir() / ".env"

    @staticmethod
    def build_env_content(settings: dict | None = None) -> str:
        """
        Produce a human-readable .env body you can copy/paste and tweak later.
        Values come from 'settings' (if provided) or current environment (fallback).
        """
        settings = settings or {}
        # pull from provided settings first, then existing env, finally a safe default
        def g(name: str, default: str = "") -> str:
            val = settings.get(name)
            if val is None or str(val).strip() == "":
                val = os.getenv(name, default)
            return str(val or "").strip()

        # common knobs used by this project
        MODEL       = g("MODEL", "ollama/qwen2.5:7b-instruct")
        API_BASE    = g("API_BASE", "http://localhost:11434")
        OPENAI_KEY  = g("OPENAI_API_KEY", "")
        ANTHROPIC   = g("ANTHROPIC_API_KEY", "")
        REPORTS     = g("GEIST_REPORTS_ROOT", str(Path.home() / ".geist"))
        # seance tuning (non-critical; defaults are good)
        DEFAULT_K   = g("SEANCE_DEFAULT_K", "6")
        RETRIEVER   = g("SEANCE_RETRIEVER", "bm25")
        BM25_K1     = g("SEANCE_BM25_K1", "1.2")
        BM25_B      = g("SEANCE_BM25_B", "0.75")
        KEY_BOOST   = g("SEANCE_KEYWORD_BOOST", "6.0")

        # Render a friendly template. Keep comments short and scannable.
        return (
            "# ─────────────────────────────────────────────────────────────\n"
            "# Geist Agent — user environment (.geist/.env)\n"
            "# This file was auto-created by 'poltergeist doctor'.\n"
            "# Safe to edit; we won't overwrite if it already exists.\n"
            "# ─────────────────────────────────────────────────────────────\n"
            "\n"
            "# ----- Core LLM routing -----\n"
            f"MODEL={MODEL}\n"
            f"API_BASE={API_BASE}\n"
            "\n"
            "# Optional providers (leave blank if unused)\n"
            f"OPENAI_API_KEY={OPENAI_KEY}\n"
            f"ANTHROPIC_API_KEY={ANTHROPIC}\n"
            "\n"
            "# Reports output root (default is ~/.geist)\n"
            f"GEIST_REPORTS_ROOT={REPORTS}\n"
            "\n"
            "# ----- Séance defaults (retrieval/chat) -----\n"
            f"SEANCE_DEFAULT_K={DEFAULT_K}\n"
            f"SEANCE_RETRIEVER={RETRIEVER}   # bm25 | jaccard\n"
            f"SEANCE_BM25_K1={BM25_K1}\n"
            f"SEANCE_BM25_B={BM25_B}\n"
            f"SEANCE_KEYWORD_BOOST={KEY_BOOST}\n"
            "\n"
            "# If you run Ollama locally, API_BASE should be http://localhost:11434\n"
            "# For OpenAI, set API_BASE=https://api.openai.com and provide OPENAI_API_KEY.\n"
            "# For Anthropic, leave MODEL to your Claude model and provide ANTHROPIC_API_KEY.\n"
            "\n"
        )

    @staticmethod
    def ensure_user_env(settings: dict | None = None) -> dict:
        """
        Ensure ~/.geist/.env exists. Never overwrites.
        Returns: { 'path': <str>, 'created': <bool> }
        """
        p = EnvUtils.user_env_path()
        if p.exists():
            return {"path": str(p), "created": False}

        body = EnvUtils.build_env_content(settings or {})
        try:
            p.write_text(body, encoding="utf-8")
            return {"path": str(p), "created": True}
        except Exception as e:
            # don't crash doctor on write trouble; just report we couldn't create it
            return {"path": str(p), "created": False, "error": str(e)}
    @staticmethod
    def load_env_for_tool() -> List[str]:
        """
        Load environment variables for Geist tools in this precedence:

        1) GEIST_ENV_FILE (explicit path, overrides)
        2) User secrets   (~/.geist/.env, %APPDATA%/Geist/.env, ~/.config/geist/.env)
                          (also accept 'env' and '.env.local') (overrides)
        3) Packaged defaults (app_root/.env, app_root/../.env, app_root/config/.env) (no override)
        4) CWD .env (soft load, no override)

        Returns: list of sources that were successfully loaded (paths as strings).
        """
        loaded: List[str] = []

        def _load(p: Path, override: bool) -> bool:
            if p.is_file():
                load_dotenv(p, override=override)
                loaded.append(str(p))
                return True
            return False

        def _variants(dirpath: Path) -> List[Path]:
            # Accept multiple common names so Windows users who created `env` are covered.
            return [dirpath / ".env", dirpath / "env", dirpath / ".env.local"]

        # 0) Resolve Geist app root
        app_root = PathUtils.geist_app_root()

        # 1) Explicit override
        explicit = os.getenv("GEIST_ENV_FILE")
        if explicit:
            _load(Path(explicit), override=True)

        # 2) User-level (override=True)
        home = Path.home()
        user_dirs: List[Path] = [home / ".geist"]

        appdata = os.getenv("APPDATA")
        if appdata:
            user_dirs.append(Path(appdata) / "Geist")

        xdg = Path(os.getenv("XDG_CONFIG_HOME", str(home / ".config")))
        user_dirs.append(xdg / "geist")

        for d in user_dirs:
            for cand in _variants(d):
                _load(cand, override=True)

        # 3) Packaged defaults (override=False)
        for d in [app_root, app_root.parent, app_root / "config"]:
            for cand in _variants(d):
                _load(cand, override=False)

        # 4) CWD (override=False)
        for cand in _variants(Path.cwd()):
            _load(cand, override=False)

        os.environ.setdefault("REPORTS_ROOT", str(Path.home() / ".geist"))
        return loaded


class PathUtils:
    @staticmethod
    def geist_app_root() -> Path:
        """
        Resolve the Geist app root from this package location (works in dev/editable and installed).
        """
        pkg_dir = Path(__file__).resolve().parent  # .../geist_agent
        repo_candidate = pkg_dir.parent            # .../src or site-packages parent
        # Prefer the parent if it looks like a repo root
        if (repo_candidate / "pyproject.toml").exists() or (repo_candidate / ".git").exists():
            return repo_candidate.parent if (repo_candidate.name == "src") else repo_candidate
        return pkg_dir

    @staticmethod
    def ensure_reports_dir(subfolder: str | None = None) -> Path:
        """
        Default reports under USER HOME (~/.geist) so it’s stable,
        writable, and consistent for both dev and installed tools.

        Override with GEIST_REPORTS_ROOT when needed.
        """
        base_str = os.getenv("GEIST_REPORTS_ROOT")
        if base_str:
            base = Path(base_str)
        else:
            base = Path.home() / ".geist" 

        out = base / subfolder if subfolder else base
        out.mkdir(parents=True, exist_ok=True)
        return out
    

# ----------[ EXTENSION PROFILES ]----------
SCAN_EXTS_FULL = {
    # Python & notebooks
    ".py", ".ipynb",
    # JS/TS stacks
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    # Web assets & docs
    ".css", ".scss", ".sass", ".html", ".htm", ".json", ".md",
    # Configs
    ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf",
    # Shell
    ".sh", ".bash", ".zsh",
    # Other langs Unveil/Ward parse or display
    ".java", ".kt", ".kts", ".c", ".h", ".hpp", ".hh", ".cc", ".cpp", ".cs",
    ".sql", ".go", ".rb", ".php", ".vue",
    # Locks & infra (Ward benefits)
    ".lock", ".tf", ".tfvars", ".pem", ".key", ".crt", ".pub", ".txt",
    # Special filenames (no suffix)
    "Dockerfile", "dockerfile",
}

SCAN_EXTS_FAST = {
    ".py",".js",".mjs",".cjs",".ts",".tsx",".jsx",
    ".java",".kt",".kts",".c",".h",".hpp",".hh",".cc",".cpp",".cs",
    ".go",".rb",".php",".sql",".vue",".html",".htm",".css",".scss",".sass",
}

SKIP_DIRS = {
    ".git", ".svn", ".hg",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "env",
    "node_modules", "dist", "build", "target", "out", ".next", ".nuxt",
    ".idea", ".vscode", ".DS_Store", ".egg-info",
}

def _preview(seq, n=6):
    seq = list(seq or [])
    head = ", ".join(map(str, seq[:n]))
    more = f" …(+{len(seq)-n})" if len(seq) > n else ""
    return head + more if head else "NONE"

def _log_walk_start(root: Path, include_exts, exclude_dirs, ignore_globs, follow_symlinks):
    print(f"▶ walk_files: root='{root}'")
    print(f"  • include_exts[{0 if include_exts is None else len(include_exts)}]={_preview(sorted(include_exts) if include_exts else [])}")
    print(f"  • exclude_dirs[{len(exclude_dirs) if exclude_dirs else 0}]={_preview(sorted(exclude_dirs) if exclude_dirs else [])}")
    print(f"  • ignore_globs[{len(ignore_globs) if ignore_globs else 0}]={_preview(ignore_globs or [])}")
    print(f"  • follow_symlinks={follow_symlinks}")

def _should_skip_dir(name: str, exclude_dirs: set[str]) -> bool:
    # quick checks for common junk/system dirs
    return (name in exclude_dirs) or name.startswith(".")

def _is_included_file(path: Path, include_exts: set[str] | None) -> bool:
    if include_exts is None:
        return True
    # Handle both “Dockerfile” (no suffix) and normal suffixes
    if path.name in include_exts:
        return True
    return path.suffix.lower() in include_exts

def _is_ignored_by_globs(rel_posix: str, ignore_globs: list[str] | None) -> bool:
    if not ignore_globs:
        return False
    # Match against the posix-style relative path and the basename
    return any(fnmatch(rel_posix, pat) or fnmatch(rel_posix.split("/")[-1], pat) for pat in ignore_globs)

def walk_files(
    root: str | Path,
    include_exts: set[str] | None = None,
    exclude_dirs: set[str] | None = None,
    ignore_globs: list[str] | None = None,
    follow_symlinks: bool = False,
):
    """
    Yield Paths for files in `root` that match extensions and ignore patterns.
    - include_exts: set of extensions (e.g., {'.py', '.js'}) or filenames (e.g., {'Dockerfile'})
    - exclude_dirs: directory names to skip anywhere in the tree
    - ignore_globs: shell-style patterns tested against the relative posix path, e.g. ['**/*.min.js', '*.lock']
    - follow_symlinks: whether to follow directory symlinks
    """
    root = Path(root).resolve()
    include_exts = include_exts or SCAN_EXTS_FULL
    exclude_dirs = exclude_dirs or SKIP_DIRS
    ignore_globs = ignore_globs or []

    _log_walk_start(root, include_exts, exclude_dirs, ignore_globs, follow_symlinks)

    # Manual stack-based walk to support follow_symlinks=True without os.walk quirks
    stack = [root]
    emitted = 0

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    name = entry.name

                    # Skip common noise and requested dirs
                    if entry.is_dir(follow_symlinks=False):
                        if _should_skip_dir(name, exclude_dirs):
                            # verbose skip log
                            # print(f"  ⤫ DIR-SKIP: {entry.path}")
                            continue
                        if entry.is_symlink() and not follow_symlinks:
                            # print(f"  ⤫ SYMLINK-DIR-SKIP: {entry.path}")
                            continue
                        stack.append(Path(entry.path))
                        continue

                    # Files
                    path = Path(entry.path)
                    try:
                        rel = path.relative_to(root).as_posix()
                    except Exception:
                        rel = path.as_posix()

                    if _is_ignored_by_globs(rel, ignore_globs):
                        # print(f"  ⤫ GLOB-IGNORE: {rel}")
                        continue
                    if not _is_included_file(path, include_exts):
                        # print(f"  ⤫ EXT-IGNORE: {rel}")
                        continue

                    emitted += 1
                    if emitted % 250 == 0:
                        print(f"  • walked {emitted} files…")
                    yield path
        except PermissionError:
            print(f"  ⚠ perm denied: {current}")
        except FileNotFoundError:
            print(f"  ⚠ gone during walk: {current}")
        except OSError as e:
            print(f"  ⚠ os error on {current}: {e}")

    print(f"✓ walk_files complete: {emitted} files")

def _prefix_ok(rel_posix: str, includes: list[str], excludes: list[str]) -> bool:
    if any(rel_posix.startswith(e.rstrip("/")) for e in excludes):
        return False
    if includes and not any(rel_posix.startswith(i.rstrip("/")) for i in includes):
        return False
    return True

def walk_files_compat(
    root: Path | str,
    include: Iterable[str],
    exclude: Iterable[str],
    exts: Optional[Iterable[str]],
    max_files: int,
) -> List[Path]:
    root = Path(root).resolve()
    include = [i.rstrip("/\\") for i in (include or [])]
    exclude = [e.rstrip("/\\") for e in (exclude or [])]
    allow = set(e.lower() for e in (exts or [])) or None  # None ⇒ use SCAN_EXTS_FULL

    stream = walk_files(
        root=root,
        include_exts=allow if allow else None,
        exclude_dirs=SKIP_DIRS,
        ignore_globs=[],          # you can add patterns later (e.g., ["**/*.min.js"])
        follow_symlinks=False,
    )
    filtered = (
        p for p in stream
        if _prefix_ok(p.relative_to(root).as_posix(), include, exclude)
    )
    return list(islice(filtered, max_files))

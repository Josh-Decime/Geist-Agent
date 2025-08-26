import re
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, List
from dotenv import load_dotenv


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

        os.environ.setdefault("REPORTS_ROOT", str(Path.home() / ".geist" / "reports"))
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
        Default reports under USER HOME (~/.geist/reports) so it’s stable,
        writable, and consistent for both dev and installed tools.

        Override with GEIST_REPORTS_ROOT when needed.
        """
        base_str = os.getenv("GEIST_REPORTS_ROOT")
        if base_str:
            base = Path(base_str)
        else:
            base = Path.home() / ".geist" / "reports"  # <— new default anchor

        out = base / subfolder if subfolder else base
        out.mkdir(parents=True, exist_ok=True)
        return out
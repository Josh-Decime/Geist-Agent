import re
import os
from pathlib import Path
from datetime import datetime
from typing import Optional


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
    """Utility for loading environment variables from .env"""

    @staticmethod
    def load_env_for_tool() -> None:
        """
        Load env in this order:
        1) POLTERGEIST_ENV (explicit .env file path)
        2) CWD/.env
        3) User config:  %APPDATA%/Poltergeist/.env  (Windows)
                         ~/.config/poltergeist/.env  (Linux/macOS)
        4) (dev) repo root two levels up from this file, if present
        """
        try:
            from dotenv import load_dotenv
        except Exception:
            return  # python-dotenv not installed

        explicit = os.getenv("POLTERGEIST_ENV")
        if explicit and Path(explicit).is_file():
            load_dotenv(explicit, override=False)
            return

        cwd_env = Path.cwd() / ".env"
        if cwd_env.is_file():
            load_dotenv(cwd_env, override=False)
            return

        if os.name == "nt":
            cfg_dir = Path(os.getenv("APPDATA", Path.home() / "AppData/Roaming")) / "Poltergeist"
        else:
            cfg_dir = Path.home() / ".config" / "poltergeist"
        user_env = cfg_dir / ".env"
        if user_env.is_file():
            load_dotenv(user_env, override=False)
            return

        here = Path(__file__).resolve()
        candidate = here.parents[2] / ".env" if len(here.parents) >= 3 else None
        if candidate and candidate.is_file():
            load_dotenv(candidate, override=False)
            return
        
class PathUtils:
    """Utility for resolving report directories"""

    @staticmethod
    def _find_repo_root_from_cwd() -> Path:
        """
        Find repo root by walking upward from CWD.
        Looks for pyproject.toml, .git, or reports folder.
        Falls back to CWD if none found.
        """
        p = Path.cwd().resolve()
        markers = {"pyproject.toml", ".git", "reports"}
        for parent in [p, *p.parents]:
            if any((parent / m).exists() for m in markers):
                return parent
        return p

    @staticmethod
    def ensure_reports_dir(subfolder: str = "scrying_reports") -> Path:
        """
        Ensure a reports directory exists.
        Order of resolution:
          1) POLTERGEIST_REPORTS_DIR env var
          2) Repo root (if found)
          3) CWD as fallback
        Returns the resolved path.
        """
        override = os.getenv("POLTERGEIST_REPORTS_DIR")
        if override:
            base = Path(override).expanduser().resolve()
        else:
            repo_root = PathUtils._find_repo_root_from_cwd()
            base = repo_root / "reports"

        out = base / subfolder
        out.mkdir(parents=True, exist_ok=True)
        return out

"""Settings read from the environment. Names match the secrets the GitHub Actions workflow already passes."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

CONTINENT_TO_TABLE = {
    "Africa": "africa",
    "Asia": "asia",
    "Europe": "europe",
    "North America": "north_america",
    "Oceania": "oceania",
    "South America": "south_america",
}
FALLBACK_TABLE = "other"


@dataclass(frozen=True)
class Settings:
    db_user: str | None
    db_password: str | None
    db_host: str | None
    db_port: str | None
    db_name: str | None
    openai_api_key: str | None
    extract_model: str
    classify_model: str
    max_extract_attempts: int
    review_queue_path: str
    max_articles: int = 0          # 0 = no limit; a small number makes a first or manual run easy to inspect
    run_report_path: str = "run_report.json"
    serper_api_key: str | None = None

    @property
    def db_ready(self) -> bool:
        return all([self.db_user, self.db_password, self.db_host, self.db_port, self.db_name])


def database_from_url(url: str | None) -> dict:
    """user, password, host, port, dbname from a postgresql:// connection string (Supabase "Connect"); {} if none."""
    if not url:
        return {}
    from urllib.parse import unquote, urlparse

    parts = urlparse(url)
    return {"user": unquote(parts.username or ""), "password": unquote(parts.password or ""), "host": parts.hostname,
            "port": str(parts.port or 5432), "dbname": (parts.path or "/postgres").lstrip("/") or "postgres"}


def load() -> Settings:
    load_dotenv()
    url = database_from_url(os.getenv("DATABASE_URL"))     # one line instead of five; the separate names still work
    return Settings(
        db_user=os.getenv("user") or url.get("user"),
        db_password=os.getenv("password") or url.get("password"),
        db_host=os.getenv("host") or url.get("host"),
        db_port=os.getenv("port") or url.get("port"),
        db_name=os.getenv("dbname") or url.get("dbname"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        extract_model=os.getenv("EXTRACT_MODEL", "gpt-6.1-terra"),
        classify_model=os.getenv("CLASSIFY_MODEL", "gpt-6-luna"),
        max_extract_attempts=int(os.getenv("MAX_EXTRACT_ATTEMPTS", "2")),
        review_queue_path=os.getenv("REVIEW_QUEUE_PATH", "review_queue.jsonl"),
        max_articles=int(os.getenv("MAX_ARTICLES") or 0),
        run_report_path=os.getenv("RUN_REPORT_PATH", "run_report.json"),
        serper_api_key=os.getenv("SERPER_API_KEY"),
    )

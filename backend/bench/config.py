"""Application configuration. Env-only (see .env.example); no telemetry, no phone-home."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BENCH_", env_file=".env", extra="ignore")

    # Core
    database_url: str = "postgresql+asyncpg://bench:bench@localhost:5432/bench"
    secret_key: str = "dev-secret-change-me"  # signs sessions, encrypts stored provider keys
    storage_dir: str = "./storage"  # uploaded documents

    # First-boot admin bootstrap (used only if no users exist)
    admin_email: str = "admin@example.com"
    admin_password: str = "bench-admin"

    # Packs auto-installed at boot (colon-separated dirs)
    packs_dir: str = "../packs"

    # Router
    router_model: str = "anthropic/claude-haiku-4-5"
    router_timeout_seconds: float = 10.0

    # Providers — keys may also be set per-workspace via the settings UI.
    # Env always wins over DB-stored credentials.
    anthropic_api_key: str = ""
    moonshot_api_key: str = ""
    openrouter_api_key: str = ""

    # Optional dynamic OpenRouter catalog fetch (static models.yaml always wins)
    openrouter_catalog: bool = True
    openrouter_referer: str = "https://github.com/bench-platform/bench"
    openrouter_title: str = "bench"


@lru_cache
def get_settings() -> Settings:
    return Settings()

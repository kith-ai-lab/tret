"""Application configuration. Env-only (see .env.example); no telemetry, no phone-home.

`environment=production` (BENCH_ENVIRONMENT) turns the shipped development
defaults into hard startup errors — see `production_config_problems` and
docs/hardening.md.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# The values shipped in .env.example / the defaults below. Refused in production.
DEFAULT_SECRET_KEY = "dev-secret-change-me"
DEFAULT_ADMIN_PASSWORD = "bench-admin"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BENCH_", env_file=".env", extra="ignore")

    # Core
    environment: str = "development"  # development | production (see boot checks)
    database_url: str = "postgresql+asyncpg://bench:bench@localhost:5432/bench"
    secret_key: str = DEFAULT_SECRET_KEY  # signs sessions, encrypts stored provider keys
    storage_dir: str = "./storage"  # uploaded documents
    cookie_secure: bool = False  # set true behind TLS (any real deployment)
    serve_frontend_dir: str = ""  # if set, serve the built SPA from this dir

    # First-boot admin bootstrap (used only if no users exist)
    admin_email: str = "admin@example.com"
    admin_password: str = DEFAULT_ADMIN_PASSWORD

    # Packs auto-installed at boot (colon-separated dirs)
    packs_dir: str = "../packs"

    # Deterministic method sandbox (bench/services/methods.py)
    # Linux + `unshare` only: run each method in an empty network namespace.
    # Logged-and-ignored elsewhere (e.g. macOS dev machines).
    methods_network_isolation: bool = True

    # Login rate limit (in-memory sliding window, per IP + email)
    login_max_attempts: int = 10
    login_window_seconds: float = 300.0

    # Router
    router_model: str = "anthropic/claude-haiku-4-5"
    router_timeout_seconds: float = 10.0

    # ── ecological / emissions accounting (bench/services/emissions.py) ───────
    # Every figure below is a heuristic. Read docs/emissions-methodology.md
    # before quoting anything derived from them; none of it is metered and none
    # of it is audit-grade.
    #
    # Grams of CO2e per kWh of electricity, used to turn a run's estimated energy
    # into an estimated carbon figure. The default is the rough world-average
    # grid intensity (~400 gCO2e/kWh); set your own region's or your provider's
    # figure for a less wrong number.
    grid_co2e_g_per_kwh: float = 400.0
    # Optional separate factor for self-hosted (local) inference, where the
    # operator buys the power and may have a site- or market-based figure (a
    # supplier mix, a PPA, on-site solar). None falls back to
    # grid_co2e_g_per_kwh. This is the factor that lands in Scope 2.
    local_grid_co2e_g_per_kwh: float | None = None
    # Power Usage Effectiveness: total facility energy / IT-load energy. 1.2 sits
    # inside the ~1.1–1.2 band hyperscalers self-report fleet-wide, and is a
    # heuristic stand-in for a facility bench cannot see.
    datacenter_pue: float = 1.2
    # PUE for self-hosted inference. A desktop or workstation has almost no
    # facility overhead — 1.05 covers fans and a share of room cooling.
    local_pue: float = 1.05
    # Amortized embodied (manufacturing) carbon per local run, in grams —
    # GHG Protocol Scope 3 Cat. 2, capital goods. Default 0 means "not counted",
    # which *understates* self-hosted inference: set it from your own hardware's
    # embodied footprint divided by its expected lifetime run count.
    embodied_g_per_run: float = 0.0
    # Model id for the frontier-baseline counterfactual. Empty auto-selects the
    # highest-energy-class curated non-local model. The comparison is a
    # same-token efficiency indicator, never an offset or a reduction claim.
    emissions_baseline_model: str = ""

    # Providers — keys may also be set per-workspace via the settings UI.
    # Env always wins over DB-stored credentials.
    anthropic_api_key: str = ""
    moonshot_api_key: str = ""
    openrouter_api_key: str = ""

    # Optional dynamic OpenRouter catalog fetch (static models.yaml always wins)
    openrouter_catalog: bool = True
    openrouter_referer: str = "https://github.com/bench-platform/bench"
    openrouter_title: str = "bench"

    # Local model server (Ollama, LM Studio, vLLM, llama.cpp server — anything
    # exposing an OpenAI-compat /v1). Enabled iff local_base_url is set; no API
    # key is required (has_key treats the configured base_url as the credential).
    local_base_url: str = ""  # e.g. http://localhost:11434/v1 for Ollama
    local_api_key: str = ""  # most local servers ignore this
    local_display_name: str = "Local"
    local_probe_tools: bool = True  # probe each discovered model for real tool support


@lru_cache
def get_settings() -> Settings:
    return Settings()


class InsecureConfigError(RuntimeError):
    """Raised at startup when environment=production but dev defaults are in use."""


def is_production(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return s.environment.strip().lower() in ("production", "prod")


def production_config_problems(settings: Settings | None = None) -> tuple[list[str], list[str]]:
    """(fatal, warnings) for a production deployment. Empty for non-production."""
    s = settings or get_settings()
    if not is_production(s):
        return [], []
    fatal: list[str] = []
    warnings: list[str] = []
    if s.secret_key == DEFAULT_SECRET_KEY or not s.secret_key.strip():
        fatal.append(
            "BENCH_SECRET_KEY is still the shipped default. Sessions would be forgeable and "
            'stored provider keys trivially decryptable. Generate one: python -c "import '
            'secrets; print(secrets.token_urlsafe(32))"'
        )
    if s.admin_password == DEFAULT_ADMIN_PASSWORD:
        fatal.append(
            "BENCH_ADMIN_PASSWORD is still the shipped default ('bench-admin'). Set a strong "
            "password before first boot (it seeds the admin user)."
        )
    if not s.cookie_secure:
        warnings.append(
            "BENCH_COOKIE_SECURE is false in production: the session cookie will be sent over "
            "plain HTTP. Terminate TLS in front of bench and set BENCH_COOKIE_SECURE=true."
        )
    return fatal, warnings


def enforce_production_safety(settings: Settings | None = None, log=None) -> None:
    """Refuse to boot a production deployment that still carries dev defaults."""
    fatal, warnings = production_config_problems(settings)
    for warning in warnings:
        if log is not None:
            log.warning("INSECURE CONFIG: %s", warning)
    if fatal:
        raise InsecureConfigError(
            "Refusing to start with BENCH_ENVIRONMENT=production:\n  - "
            + "\n  - ".join(fatal)
            + "\nSee docs/hardening.md."
        )

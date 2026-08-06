"""Application configuration. Env-only (see .env.example); no telemetry, no phone-home.

`environment=production` (BENCH_ENVIRONMENT) turns the shipped development
defaults into hard startup errors — see `production_config_problems` and
docs/hardening.md.
"""
import json
import logging
import math
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("bench")

# The values shipped in .env.example / the defaults below. Refused in production.
DEFAULT_SECRET_KEY = "dev-secret-change-me"
DEFAULT_ADMIN_PASSWORD = "bench-admin"

# ── GHG Protocol Scope 2 basis labels ────────────────────────────────────────
# These live here, rather than in bench/services/emissions.py where the rest of
# the emissions vocabulary lives, for one reason: Settings has to *validate*
# against them, and emissions.py imports this module. emissions.py re-exports
# the same names, so it remains the module a reader goes to for the meaning.
#
# location_based describes the physical grid that served the load; market_based
# describes contractual renewable claims (PPAs, RECs, GOs). They answer
# different questions, are not interchangeable, and may never be summed.
GRID_BASIS_LOCATION = "location_based"
GRID_BASIS_MARKET = "market_based"
GRID_BASIS_UNSPECIFIED = "unspecified"
GRID_BASES = (GRID_BASIS_LOCATION, GRID_BASIS_MARKET, GRID_BASIS_UNSPECIFIED)

# Provider names BENCH_GRID_FACTORS may be keyed by. Used *only* to warn about a
# probable typo: an unrecognised key is kept, never rejected, because the catalog
# gains providers over time and a hard failure would make bench unbootable on a
# config that was correct yesterday. Kept as a literal rather than read from the
# catalog because providers/catalog.py imports this module.
GRID_FACTOR_PROVIDERS = ("local", "anthropic", "kimi", "openrouter")

# An operator's label is a note next to a number, not a description. Long enough
# for "Ontario grid, IESO 2024" and short enough to render in a table cell.
GRID_FACTOR_LABEL_MAX = 80


class GridFactor(BaseModel):
    """One operator-configured grid carbon intensity, keyed by provider name.

    `extra="forbid"` is deliberate: a mistyped `gCO2e_per_kwh` that was silently
    ignored would leave the operator believing they had configured a factor while
    bench quietly applied the global default. A rejected boot is the kinder
    failure for a typo *inside* an entry, where there is nothing to guess.
    """

    model_config = ConfigDict(extra="forbid")

    # gCO2e per kWh. Must be a positive finite number: zero would claim
    # carbon-free electricity, which no grid delivers and no operator can
    # substantiate from a supplier disclosure, and inf/nan would poison every
    # figure derived from it.
    g_per_kwh: float
    # GHG Protocol basis of the factor above. Defaults to unspecified rather than
    # to location_based: bench does not know what an operator's own number
    # represents, and guessing a basis is the one thing it must not do here.
    basis: str = GRID_BASIS_UNSPECIFIED
    # Free text shown next to the factor in the provenance surfaces — where the
    # number came from, in the operator's own words ("Ontario grid, IESO 2024",
    # "provider PPA disclosure"). Optional; blank means none.
    label: str | None = None

    @field_validator("g_per_kwh", mode="after")
    @classmethod
    def _positive_and_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("g_per_kwh must be a finite number, not inf or nan")
        if value <= 0:
            raise ValueError(
                f"g_per_kwh must be greater than 0, got {value}. A zero or negative grid "
                "factor would claim electricity with no (or negative) emissions."
            )
        return value

    @field_validator("basis", mode="before")
    @classmethod
    def _known_basis(cls, value):
        basis = (str(value) if value is not None else "").strip().lower()
        if not basis:
            return GRID_BASIS_UNSPECIFIED
        if basis not in GRID_BASES:
            raise ValueError(
                f"basis must be one of {', '.join(GRID_BASES)}; got {value!r}. "
                "location_based and market_based are not interchangeable, so bench "
                "will not accept a label it cannot interpret."
            )
        return basis

    @field_validator("label", mode="before")
    @classmethod
    def _short_label(cls, value):
        if value is None:
            return None
        label = str(value).strip()
        if not label:
            return None
        if len(label) > GRID_FACTOR_LABEL_MAX:
            raise ValueError(
                f"label must be at most {GRID_FACTOR_LABEL_MAX} characters "
                f"({len(label)} given) — it is rendered in a table cell next to the "
                "factor, not a place for the methodology"
            )
        return label


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
    # Every figure below is an estimate, calibrated against published data where
    # published data exists. Read docs/emissions-methodology.md before quoting
    # anything derived from them; none of it is metered and none of it is
    # audit-grade. Each default's source, date and uncertainty is recorded on
    # every run in `energy_accounting["factors"]`.
    #
    # Grams of CO2e per kWh of electricity, used to turn a run's estimated energy
    # into an estimated carbon figure. 470 is the IEA's 2024 global power-sector
    # average (Electricity 2025 reports ~460–480; 470 is the midpoint). Set your
    # own region's or supplier's figure for a less wrong number — eGRID
    # subregions span more than 10x.
    grid_co2e_g_per_kwh: float = 470.0
    # GHG Protocol Scope 2 basis of the factor above: location_based (the
    # physical grid that served the load), market_based (contractual renewable
    # claims — PPAs, RECs, GOs), or unspecified. The two are not interchangeable
    # and must never be summed, so the label is recorded per run. The shipped
    # default is an IEA physical-grid average, hence location_based.
    grid_co2e_basis: str = "location_based"
    # Per-provider / per-deployment grid factors, as JSON keyed by bench provider
    # name (local | anthropic | kimi | openrouter):
    #
    #   BENCH_GRID_FACTORS='{"local":{"g_per_kwh":42,"basis":"location_based",
    #                                 "label":"Ontario grid, IESO 2024"},
    #                        "anthropic":{"g_per_kwh":120,"basis":"market_based",
    #                                     "label":"provider PPA disclosure"}}'
    #
    # This is CONFIGURATION, never inference. bench does not and will not derive a
    # region from an IP address: for a cloud API call the caller's location says
    # nothing about which data centre served the request, providers do not
    # disclose the serving region, and OpenRouter routes to whichever upstream has
    # capacity. Location is knowable only when the operator knows it — they
    # self-host in a known place, or they pin a provider to a region — so it comes
    # from them. No network call is involved either way.
    #
    # Precedence: this map (by provider) → local_grid_* below when the run is
    # self-hosted → grid_co2e_g_per_kwh. Every run records which of the three
    # applied. An unrecognised provider name warns at startup and is kept; an
    # unknown key *inside* an entry is a hard error (see GridFactor).
    grid_factors: dict[str, GridFactor] | None = None
    # LEGACY, and kept working exactly as it always has: an optional separate
    # factor for self-hosted (local) inference, where the operator buys the power
    # and may have a site- or market-based figure (a supplier mix, a PPA, on-site
    # solar). None falls back to grid_co2e_g_per_kwh. This is the factor that
    # lands in Scope 2. Superseded by grid_factors["local"], which is strictly
    # more expressive (it carries a label); prefer that in new configuration.
    local_grid_co2e_g_per_kwh: float | None = None
    # Basis of the local factor above. Defaults to unspecified because bench
    # cannot know what an operator's own number represents — say which it is.
    local_grid_co2e_basis: str = "unspecified"
    # Power Usage Effectiveness: total facility energy / IT-load energy.
    # 1.2 for hyperscaler cloud sits *above* every self-report (Google 1.09 in
    # its 2025 Environmental Report, AWS 1.15, Microsoft 1.16 FY2024) and well
    # below the 1.56 industry average, i.e. deliberately conservative for a
    # facility bench cannot see.
    datacenter_pue: float = 1.2
    # PUE for a self-hosted workstation. A desktop has almost no facility
    # overhead — 1.05 covers fans and a share of room cooling.
    local_pue: float = 1.05
    # PUE for self-hosted inference in a real machine room: 1.56, the Uptime
    # Institute 2024 Global Data Center Survey industry average across 879
    # operators. An on-prem facility is a small data centre and must not borrow
    # a hyperscaler's number.
    onprem_pue: float = 1.56
    # Which of the two figures above self-hosted runs use: workstation |
    # onprem_datacenter. Anything else falls back to workstation.
    local_deployment_profile: str = "workstation"
    # Amortized embodied (manufacturing) carbon per local run, in grams —
    # GHG Protocol Scope 3 Cat. 2, capital goods. Default 0 means "not counted",
    # which *understates* self-hosted inference: set it from your own hardware's
    # embodied footprint divided by its expected lifetime run count
    # (bench.services.emissions.amortized_embodied_g_per_run computes one from
    # cited constants).
    embodied_g_per_run: float = 0.0
    # Multiplicative uncertainty band around every reported figure: low =
    # central/2.5, high = central x 2.5. A JUDGMENT BAND matching field practice
    # (Green Algorithms claims order-of-magnitude correctness; Boavizta states
    # 30–50%; spec-based estimation validates at -40%/+40%), never a confidence
    # interval and never a standard deviation. Values below 1 are clamped to 1.
    uncertainty_band_low: float = 2.5
    uncertainty_band_high: float = 2.5
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

    @field_validator("local_grid_co2e_g_per_kwh", mode="before")
    @classmethod
    def _blank_means_unset(cls, value):
        """An empty value means "not set" — fall back to grid_co2e_g_per_kwh.

        Environment variables have no way to say None: a blank line in .env, or a
        `${VAR:-}` interpolation in docker-compose.yml for a knob the operator
        never set, both arrive as "". Without this, that empty string is a float
        parse error and the backend refuses to boot — and hardcoding a number in
        compose instead would silently break the documented fallback (local
        inference would keep reporting 400 g/kWh after the operator set their own
        `BENCH_GRID_CO2E_G_PER_KWH`).
        """
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("grid_factors", mode="before")
    @classmethod
    def _parse_grid_factors(cls, value):
        """Blank means "not set"; a JSON object means one entry per provider.

        pydantic-settings decodes a complex field's env value as JSON before it
        reaches here, so a well-formed `BENCH_GRID_FACTORS` arrives already
        parsed. What still arrives as a string is (a) a blank value — the same
        `${VAR:-}` case `_blank_means_unset` exists for, which must mean "not set"
        rather than a parse error, and (b) malformed JSON, which is reported as
        such instead of as pydantic's generic "not a valid dictionary".

        Provider names are lower-cased and stripped so `Anthropic` and
        ` anthropic ` are the same key the catalog uses. A name bench does not
        recognise is KEPT and warned about, not rejected: the catalog gains
        providers over time, and refusing to boot on a stale config would be a
        worse failure than an entry that lies dormant until its provider exists.
        """
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                value = json.loads(text)
            except ValueError as exc:
                raise ValueError(
                    "BENCH_GRID_FACTORS must be a JSON object keyed by provider name, e.g. "
                    '\'{"local":{"g_per_kwh":42,"basis":"location_based"}}\' — '
                    f"could not parse it as JSON: {exc}"
                ) from exc
        if not isinstance(value, dict):
            raise ValueError(
                "BENCH_GRID_FACTORS must be a JSON object keyed by provider name "
                f"(local | anthropic | kimi | openrouter), got {type(value).__name__}"
            )
        entries: dict = {}
        for key, entry in value.items():
            provider = str(key).strip().lower()
            if not provider:
                raise ValueError("BENCH_GRID_FACTORS contains an empty provider name")
            if provider in entries:
                raise ValueError(
                    f"BENCH_GRID_FACTORS names provider {provider!r} more than once"
                )
            entries[provider] = entry
        unknown = sorted(set(entries) - set(GRID_FACTOR_PROVIDERS))
        if unknown:
            log.warning(
                "BENCH_GRID_FACTORS names provider(s) bench does not recognise: %s. "
                "Known providers: %s. The entries are kept and will apply if the catalog "
                "gains those providers, so check for a typo — until then those factors "
                "are never used and runs fall back to BENCH_GRID_CO2E_G_PER_KWH.",
                ", ".join(unknown),
                ", ".join(GRID_FACTOR_PROVIDERS),
            )
        return entries


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

"""Application configuration. Env-only (see .env.example); no telemetry, no phone-home.

`environment=production` (TRET_ENVIRONMENT) turns the shipped development
defaults into hard startup errors — see `production_config_problems` and
docs/hardening.md.
"""
import json
import logging
import math
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("tret")

# ── egress modes (tret/net/policy.py) ────────────────────────────────────────
# Kept here, not imported from tret.net, because Settings has to *validate*
# against them and tret.net imports this module. policy.py re-exports the same
# names and remains the place the meaning is written down.
EGRESS_MODES = ("off", "replay", "on")
# Spellings an operator plausibly types for a switch. Mapped rather than rejected
# because `TRET_EGRESS_RESEARCH=true` silently reading as "off" is the worst of
# both worlds: the safe outcome, arrived at by ignoring what they wrote.
_EGRESS_ALIASES = {
    "true": "on", "1": "on", "yes": "on", "enabled": "on",
    "false": "off", "0": "off", "no": "off", "disabled": "off", "": "off",
}

# The values shipped in .env.example / the defaults below. Refused in production.
DEFAULT_SECRET_KEY = "dev-secret-change-me"
DEFAULT_ADMIN_PASSWORD = "tret-admin"

# ── GHG Protocol Scope 2 basis labels ────────────────────────────────────────
# These live here, rather than in tret/services/emissions.py where the rest of
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

# Provider names TRET_GRID_FACTORS may be keyed by. Used *only* to warn about a
# probable typo: an unrecognised key is kept, never rejected, because the catalog
# gains providers over time and a hard failure would make tret unbootable on a
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
    tret quietly applied the global default. A rejected boot is the kinder
    failure for a typo *inside* an entry, where there is nothing to guess.
    """

    model_config = ConfigDict(extra="forbid")

    # gCO2e per kWh. Must be a positive finite number: zero would claim
    # carbon-free electricity, which no grid delivers and no operator can
    # substantiate from a supplier disclosure, and inf/nan would poison every
    # figure derived from it.
    g_per_kwh: float
    # GHG Protocol basis of the factor above. Defaults to unspecified rather than
    # to location_based: tret does not know what an operator's own number
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
                "location_based and market_based are not interchangeable, so tret "
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
    model_config = SettingsConfigDict(env_prefix="TRET_", env_file=".env", extra="ignore")

    # Core
    environment: str = "development"  # development | production (see boot checks)
    database_url: str = "postgresql+asyncpg://tret:tret@localhost:5432/tret"
    secret_key: str = DEFAULT_SECRET_KEY  # signs sessions, encrypts stored provider keys
    storage_dir: str = "./storage"  # uploaded documents
    cookie_secure: bool = False  # set true behind TLS (any real deployment)
    serve_frontend_dir: str = ""  # if set, serve the built SPA from this dir
    # Skip the boot-time `alembic upgrade head` step (tret/db/migrate.py), for
    # operators who migrate from a separate deploy step under a privileged role.
    # tret then assumes the database is already at head and fails on the first
    # query that needs a missing column — docs/upgrading.md, docs/hardening.md §8.
    skip_migrations: bool = False

    # ── single-instance enforcement (tret/services/instance_lock.py) ──────────
    # fly.toml says "Do NOT scale horizontally" in a comment; this is what
    # actually checks it. Postgres-only: a session-level pg_try_advisory_lock
    # taken at boot and held for the process lifetime, since the run event bus
    # is in-process and a second live instance cannot share it. warn (default)
    # logs at ERROR and keeps booting — self-hosted operators are not all
    # running Postgres in a way that makes this actionable, and a false
    # positive must never take a deployment down. strict refuses to boot
    # instead, for an operator who wants that guaranteed. off skips the check
    # (and the lock query) entirely. See docs/hardening.md.
    instance_lock: str = "warn"  # warn | strict | off
    # How long a losing process retries before deciding the lock is genuinely
    # held rather than a Fly deploy handover's old machine still finishing up.
    # 90s covers a normal handover plus the ~30s the lock connection's own
    # keepalive tuning takes to reap a crashed prior holder's dangling
    # connection (see instance_lock.py's module docstring).
    instance_lock_wait_seconds: float = 90.0

    @field_validator("instance_lock", mode="before")
    @classmethod
    def _known_instance_lock_mode(cls, value):
        candidate = str(value if value is not None else "").strip().lower()
        if candidate not in ("warn", "strict", "off"):
            raise ValueError(f"instance_lock must be one of warn|strict|off, got {value!r}")
        return candidate

    # Multi-tenant SaaS mode (tret_cloud; OIDC login lands in a later phase).
    # False is every open-source/self-hosted deployment: bootstrap seeds one
    # workspace, every user is a member of it, and the demo content (climate
    # pack, sample harnesses) is seeded as it always has been. True changes
    # what bootstrap seeds for a *new* workspace (no demo content — a paying
    # tenant does not want a stranger's sample project) and is the switch OIDC
    # provisioning (Phase B) reads to decide whether a new user gets their own
    # personal workspace or joins the sole one. Tenancy primitives themselves
    # (memberships, workspace-scoped queries, the switch endpoint) are always
    # active — this flag only ever changes bootstrap's seeding choice, never
    # what a self-hosted deployment's single workspace looks like.
    multi_tenant: bool = False

    # Cap on how many *team* workspaces one user may own, via
    # `POST /api/workspaces` (api/workspaces.py::create_team_workspace).
    # 0 (the default) is unlimited — every self-hosted deployment, and a
    # multi-tenant one that hasn't chosen to bound this. Counted by
    # membership `role == 'owner'` in a `kind='team'` workspace, since that
    # is exactly what creating one grants the caller — a user who owns N
    # already and is at the cap gets a 403, not a 500 from an unbounded table
    # scan an operator never asked to allow.
    max_team_workspaces_per_user: int = 0

    # ── login (tret/api/auth.py, tret/api/oidc.py) ────────────────────────────
    # password (default — every self-hosted deployment) | oidc (single sign-on
    # only; POST /api/auth/login and every password-setting endpoint refuse
    # with 403) | both (password stays available alongside SSO, e.g. during a
    # migration window). Setting this to oidc/both with `oidc_issuer` below
    # left blank disables password login with nothing to replace it, so pair
    # the two — nothing here validates that combination, on purpose: this
    # flag and the router mount (main.py) are deliberately independent
    # switches, the same way `multi_tenant` and OIDC are.
    auth_mode: str = "password"  # password | oidc | both

    # Generic OIDC login (tret/api/oidc.py) via authlib — deliberately never an
    # Auth0 SDK, so any spec-compliant issuer works (Auth0, Okta, Keycloak,
    # Google...). The router mounts only when this is set (main.py); every
    # self-hosted deployment leaves it blank and gets exactly today's
    # password-only login.
    oidc_issuer: str = ""  # e.g. your-tenant.us.auth0.com — scheme optional
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # The callback URL tret registers with the IdP, e.g.
    # https://cloud.tret.kithailab.com/api/auth/oidc/callback. Preferred over
    # deriving one from the incoming request: tret does not trust
    # X-Forwarded-Proto any more than api/auth.py's login limiter trusts
    # X-Forwarded-For, so behind Fly's TLS-terminating proxy `request.url`
    # alone reports `http://` and the IdP will refuse a redirect_uri mismatch.
    # Left blank, tret falls back to a same-origin guess from the request —
    # fine for a bare local/dev OIDC setup, wrong behind any real proxy.
    oidc_redirect_url: str = ""
    # Where the browser lands after logout, once tret's own session cookie is
    # already cleared. Verbatim if set. Left blank in `oidc` mode, tret builds
    # Auth0's own non-standard `/v2/logout` URL as a convenience fallback (see
    # api/auth.py::_oidc_logout_url) — there is no OIDC-standard end-session
    # endpoint to fall back to, so any other IdP must set this explicitly.
    oidc_logout_url: str = ""
    # Allowlist for who a fresh OIDC sign-in may JIT-provision an account
    # for (services/identity.py::match_or_provision) — comma-separated
    # domains ("kithailab.com,example.org"), matched against the part of the
    # verified email after "@". Empty (the default) means no domain
    # restriction of its own; self-host still falls back to requiring an
    # invitation in that case (see match_or_provision's own docstring) —
    # this is the operator-facing knob for a multi-tenant deployment (or a
    # self-host one) that wants to say "anyone at these domains", instead.
    # Checked before every JIT-provision, never before matching an sub/email
    # already on file — an existing linked account must keep signing in even
    # if this list changes later.
    oidc_allowed_email_domains: list[str] | None = None

    @field_validator("oidc_allowed_email_domains", mode="before")
    @classmethod
    def _parse_oidc_allowed_email_domains(cls, value):
        """Comma-separated domains, same shape and same None/blank-means-
        empty handling as `extensions` above."""
        if value is None:
            return []
        if isinstance(value, str):
            return [d.strip().lower() for d in value.split(",") if d.strip()]
        return value

    @field_validator("auth_mode", mode="before")
    @classmethod
    def _known_auth_mode(cls, value):
        candidate = str(value if value is not None else "").strip().lower()
        if candidate not in ("password", "oidc", "both"):
            raise ValueError(f"auth_mode must be one of password|oidc|both, got {value!r}")
        return candidate

    # This deployment's own public base URL — the one place it has to be
    # written down for a link that is *emailed out* rather than followed by a
    # browser already on the site (an invite link, api/workspaces.py). Every
    # other cross-origin URL in the app (oidc_redirect_url, static asset URLs)
    # is either same-origin already or a destination tret is *reaching*, not
    # one it is *handing out* — this is the first setting that needs the
    # latter. e.g. https://cloud.tret.kithailab.com.
    app_url: str = "http://localhost:8000"

    # ── invite email (tret/services/mailer.py) ────────────────────────────────
    # off (default) | resend | smtp. Every self-hosted deployment ships off:
    # POST /api/workspaces/{id}/invites still works with nothing configured
    # here — it always returns the invite link itself, so copy-link never
    # depends on this.
    email_mode: str = "off"
    # Resend (https://resend.com): TRET_EMAIL_MODE=resend posts through
    # tret.net, same egress pattern as api/oidc.py — see mailer.py.
    resend_api_key: str = ""
    # The From address every invite email is sent as, either mode. Must be a
    # verified sending domain in Resend for `resend` mode; any address your
    # relay accepts for `smtp`.
    email_from: str = ""
    # Self-hosted SMTP relay. `smtp_tls` issues STARTTLS after connecting —
    # leave it on for any real relay; only a local/dev relay with no TLS
    # support needs it off.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_tls: bool = True

    @field_validator("email_mode", mode="before")
    @classmethod
    def _known_email_mode(cls, value):
        candidate = str(value if value is not None else "").strip().lower()
        if candidate not in ("off", "resend", "smtp"):
            raise ValueError(f"email_mode must be one of off|resend|smtp, got {value!r}")
        return candidate

    # In-process extension modules (tret/engine/extensions.py), comma-separated
    # dotted module names, e.g. TRET_EXTENSIONS=tret_billing.extension. Each
    # module's `register(ext)` is called at boot with the shared ExtensionAPI.
    # Empty (the default, and what every open-source deployment ships) means
    # the extension seam is entirely inert: no router mounted, every pre-run
    # gate allowed, no post-run hook run.
    #
    # Typed `list[str] | None` rather than `list[str]` on purpose: pydantic-
    # settings tries to JSON-decode a non-optional complex field's env value and
    # treats a parse failure as fatal, which a plain comma-separated string
    # always is. The union form tolerates the failure and hands the raw string
    # to `_parse_extensions` below instead — the same reason `grid_factors`
    # above is `dict[str, GridFactor] | None` rather than plain `dict`.
    extensions: list[str] | None = None

    # First-boot admin bootstrap (used only if no users exist)
    admin_email: str = "admin@example.com"
    admin_password: str = DEFAULT_ADMIN_PASSWORD

    # Packs auto-installed at boot (colon-separated dirs)
    packs_dir: str = "../packs"

    # Whether every *new* workspace — self-host's boot-time Default workspace,
    # a multi-tenant JIT personal workspace (services/identity.py), or a Phase C
    # team workspace (api/workspaces.py) — gets every pack found in
    # TRET_PACKS_DIR installed. True (the default) is deliberate for every
    # deployment mode, multi-tenant included: a signup should land on a working
    # pack, not an empty shell. This is independent of `seed_demo_content`
    # (services/workspace.py), which is about *sample* content beyond the pack
    # (a "Sample Engagement" project name/description, the premium-tier Climate
    # Analyst harness) and still follows TRET_MULTI_TENANT by default. Set
    # false to ship every new workspace pack-less regardless of mode.
    seed_default_packs: bool = True

    # Base URL of the pack marketplace registry (tret-cloud's `/api/marketplace`
    # API) that `GET/POST /api/packs/registry/*` (api/packs.py) proxy through
    # to for Find/Install — see that module's own note on the invariant this
    # protects: the RUN path (executing a task with an already-installed pack)
    # never reads this setting at all, only Find/Install/Submit do.
    #
    # OPT-IN, empty by default: unset, the registry client is disabled
    # entirely (those endpoints 503 rather than doing nothing quietly, and
    # the Find tab / update badges degrade to a friendly empty state) and
    # tret makes zero marketplace network calls — no installed-pack slug,
    # search query, or version check ever leaves the deployment. README's
    # "No telemetry, ever" would be false for a self-hoster if this defaulted
    # to Kith's own registry, so it does not: an operator who wants Find/
    # Install sets `TRET_PACK_REGISTRY_URL` themselves, to Kith's registry
    # (https://cloud.tret.kithailab.com/api/marketplace) or a private mirror.
    # tret-cloud's own deployment (fly.toml) sets this for itself — the
    # hosted product opts in on its own behalf, self-host never opts in for
    # the operator.
    pack_registry_url: str = ""

    # ── outbound network (tret/net/) ─────────────────────────────────────────
    # Egress is five classes, not one boolean, because tret must reach an LLM
    # provider to do anything and "no internet" is a different deployment from
    # "no *research* internet". Every switch here narrows: `egress` is the master,
    # each class switch narrows it further, and the settings API can narrow again
    # at runtime but never widen. See tret/net/policy.py and docs/hardening.md §9.
    egress: str = "on"  # on | off — off is the air-gapped deployment (local models only)
    egress_provider: str = "on"  # LLM API calls
    egress_catalog: str = "on"  # OpenRouter model list, provider key validation
    # A self-hosted model server at TRET_LOCAL_BASE_URL. Deliberately NOT
    # narrowed by `egress` above: a call to a model server on your own network
    # never leaves the deployment, and an air-gapped tret that could not reach
    # one would do nothing at all. With TRET_EGRESS=off the local class instead
    # *requires* that host to resolve to a private address, so the exemption is
    # checked rather than taken on the word of a variable name.
    egress_local: str = "on"
    # Web search and page fetch. OFF by default: it is the only class whose
    # destination is chosen by a model, from text that may have come from an
    # uploaded document. `replay` serves fetches from snapshots already taken and
    # refuses new ones, which is how a benchmark or an audit re-runs offline.
    egress_research: str = "off"  # off | replay | on
    # Comma-separated hosts the research class may reach (subdomains included).
    # EMPTY MEANS THE PUBLIC WEB — a general web search cannot work against an
    # allowlist, and pretending otherwise would be worse than saying so. Set it
    # to turn research into a genuine allowlist. The configured search endpoint
    # does NOT need to be listed here — it rides its own `search` egress class
    # (tret/net/policy.py CLASS_SEARCH), not this one.
    egress_research_allow_hosts: str = ""
    egress_research_max_bytes: int = 2_000_000  # per fetched page
    egress_research_timeout_seconds: float = 20.0
    egress_research_max_fetches_per_run: int = 10
    # Force every outbound request through one proxy. The app-level allowlist is
    # a deterrent; a proxy that the workload cannot bypass is a boundary.
    egress_proxy: str = ""  # e.g. http://egress-proxy.internal:3128

    # Web search backend (tret/net/search/). Empty = no search: web_search is
    # registered but tells the model it is unconfigured, so a harness that lists
    # the tool still runs. `searxng` is the self-hosted option, which keeps the
    # query itself inside the deployment.
    search_provider: str = ""  # "" | brave | searxng
    search_api_key: str = ""  # Brave: the subscription token
    searxng_base_url: str = ""  # e.g. http://searxng.internal:8080

    # Deterministic method sandbox (tret/services/methods.py)
    # Linux + `unshare` only: run each method in an empty network namespace.
    # Logged-and-ignored elsewhere (e.g. macOS dev machines).
    methods_network_isolation: bool = True

    # Login rate limit. `login_max_attempts` is the per-(source, account) limit;
    # a second, account-wide bucket is allowed ACCOUNT_BURST_MULTIPLE times that,
    # so the limit survives a reverse proxy collapsing every peer into one
    # address. See tret/api/auth.py::_login_buckets.
    login_max_attempts: int = 10
    login_window_seconds: float = 300.0

    # Router
    router_model: str = "anthropic/claude-haiku-4-5"
    router_timeout_seconds: float = 10.0

    # Headless CLI ledger (`tret run`, tret/local_run.py). One JSON line is
    # appended here per run: what it cost, what it's estimated to have emitted,
    # which model, which ledger id. Empty means the default, ~/.tret/ledger.jsonl
    # — not written here as a literal default because it must expand the
    # *invoking user's* home directory, not one baked in at import time.
    ledger_path: str = ""

    # ── ecological / emissions accounting (tret/services/emissions.py) ────────
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
    # Per-provider / per-deployment grid factors, as JSON keyed by tret provider
    # name (local | anthropic | kimi | openrouter):
    #
    #   TRET_GRID_FACTORS='{"local":{"g_per_kwh":42,"basis":"location_based",
    #                                 "label":"Ontario grid, IESO 2024"},
    #                        "anthropic":{"g_per_kwh":120,"basis":"market_based",
    #                                     "label":"provider PPA disclosure"}}'
    #
    # This is CONFIGURATION, never inference. tret does not and will not derive a
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
    # Basis of the local factor above. Defaults to unspecified because tret
    # cannot know what an operator's own number represents — say which it is.
    local_grid_co2e_basis: str = "unspecified"
    # Power Usage Effectiveness: total facility energy / IT-load energy.
    # 1.2 for hyperscaler cloud sits *above* every self-report (Google 1.09 in
    # its 2025 Environmental Report, AWS 1.15, Microsoft 1.16 FY2024) and well
    # below the 1.56 industry average, i.e. deliberately conservative for a
    # facility tret cannot see.
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
    # (tret.services.emissions.amortized_embodied_g_per_run computes one from
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

    # ── workspace connections (tret/services/connections.py, tret/api/connections.py) ──
    # OAuth client credentials for the two Phase 0 providers (services/connections.py's
    # provider keys, exactly: "gdrive", "m365"). All optional, all blank by default —
    # every self-hosted deployment ships with connections entirely unconfigured, and
    # `GET /api/connections/providers` reports each as `configured: false` until either
    # these are set or an extension supplies a client via `ext.add_oauth_client_provider`
    # (tret_cloud does this for the hosted product; see engine/extensions.py). Env
    # always wins over the extension hook, same precedence as the provider API keys
    # above.
    gdrive_client_id: str = ""
    gdrive_client_secret: str = ""
    m365_client_id: str = ""
    m365_client_secret: str = ""
    # Google Picker developer key + Cloud project number — handed to the frontend
    # picker verbatim via `GET /api/connections/providers` (never proxied through
    # this backend; the browser talks to Google's picker JS directly). Blank means
    # the gdrive provider's `picker` field is omitted even if a client is configured,
    # so "connect works" and "the in-app picker works" can be set up independently.
    gdrive_picker_api_key: str = ""
    gdrive_app_id: str = ""

    # Providers — keys may also be set per-workspace via the settings UI.
    # Env always wins over DB-stored credentials.
    anthropic_api_key: str = ""
    moonshot_api_key: str = ""
    openrouter_api_key: str = ""

    # Optional dynamic OpenRouter catalog fetch (static models.yaml always wins)
    openrouter_catalog: bool = True
    openrouter_referer: str = "https://github.com/tret-platform/tret"
    openrouter_title: str = "tret"

    # Local model server (Ollama, LM Studio, vLLM, llama.cpp server — anything
    # exposing an OpenAI-compat /v1). Enabled iff local_base_url is set; no API
    # key is required (has_key treats the configured base_url as the credential).
    local_base_url: str = ""  # e.g. http://localhost:11434/v1 for Ollama
    local_api_key: str = ""  # most local servers ignore this
    local_display_name: str = "Local"
    local_probe_tools: bool = True  # probe each discovered model for real tool support

    @field_validator(
        "egress", "egress_provider", "egress_catalog", "egress_local", "egress_research", mode="before"
    )
    @classmethod
    def _egress_mode(cls, value):
        """Normalize an egress switch, and refuse a spelling with no meaning.

        A typo in a kill switch is the one config error that must not fail quiet:
        `TRET_EGRESS_RESEARCH=of` reading as "off" happens to be safe, while
        `TRET_EGRESS=of` reading as "off" takes the whole deployment down for a
        reason nobody can see. Both fail at boot instead.
        """
        candidate = str(value if value is not None else "").strip().lower()
        candidate = _EGRESS_ALIASES.get(candidate, candidate)
        if candidate not in EGRESS_MODES:
            raise ValueError(
                f"must be one of {', '.join(EGRESS_MODES)} (got {value!r}). "
                "`replay` is meaningful only for TRET_EGRESS_RESEARCH."
            )
        return candidate

    @field_validator("extensions", mode="before")
    @classmethod
    def _parse_extensions(cls, value):
        """Comma-separated module names, matching `egress_research_allow_hosts`
        rather than JSON — there is no nesting here to justify JSON's
        punctuation.

        None (unset) and a blank string both mean "no extensions", not a parse
        error: a blank `${TRET_EXTENSIONS:-}` interpolation in docker-compose.yml
        for an operator who never set the var must boot exactly like the var
        being absent altogether.
        """
        if value is None:
            return []
        if isinstance(value, str):
            return [name.strip() for name in value.split(",") if name.strip()]
        return value

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
        `TRET_GRID_CO2E_G_PER_KWH`).
        """
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("grid_factors", mode="before")
    @classmethod
    def _parse_grid_factors(cls, value):
        """Blank means "not set"; a JSON object means one entry per provider.

        pydantic-settings decodes a complex field's env value as JSON before it
        reaches here, so a well-formed `TRET_GRID_FACTORS` arrives already
        parsed. What still arrives as a string is (a) a blank value — the same
        `${VAR:-}` case `_blank_means_unset` exists for, which must mean "not set"
        rather than a parse error, and (b) malformed JSON, which is reported as
        such instead of as pydantic's generic "not a valid dictionary".

        Provider names are lower-cased and stripped so `Anthropic` and
        ` anthropic ` are the same key the catalog uses. A name tret does not
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
                    "TRET_GRID_FACTORS must be a JSON object keyed by provider name, e.g. "
                    '\'{"local":{"g_per_kwh":42,"basis":"location_based"}}\' — '
                    f"could not parse it as JSON: {exc}"
                ) from exc
        if not isinstance(value, dict):
            raise ValueError(
                "TRET_GRID_FACTORS must be a JSON object keyed by provider name "
                f"(local | anthropic | kimi | openrouter), got {type(value).__name__}"
            )
        entries: dict = {}
        for key, entry in value.items():
            provider = str(key).strip().lower()
            if not provider:
                raise ValueError("TRET_GRID_FACTORS contains an empty provider name")
            if provider in entries:
                raise ValueError(
                    f"TRET_GRID_FACTORS names provider {provider!r} more than once"
                )
            entries[provider] = entry
        unknown = sorted(set(entries) - set(GRID_FACTOR_PROVIDERS))
        if unknown:
            log.warning(
                "TRET_GRID_FACTORS names provider(s) tret does not recognise: %s. "
                "Known providers: %s. The entries are kept and will apply if the catalog "
                "gains those providers, so check for a typo — until then those factors "
                "are never used and runs fall back to TRET_GRID_CO2E_G_PER_KWH.",
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
            "TRET_SECRET_KEY is still the shipped default. Sessions would be forgeable and "
            'stored provider keys trivially decryptable. Generate one: python -c "import '
            'secrets; print(secrets.token_urlsafe(32))"'
        )
    if s.admin_password == DEFAULT_ADMIN_PASSWORD:
        fatal.append(
            "TRET_ADMIN_PASSWORD is still the shipped default ('tret-admin'). Set a strong "
            "password before first boot (it seeds the admin user)."
        )
    if s.egress_research == "on" and not s.egress_research_allow_hosts.strip():
        warnings.append(
            "TRET_EGRESS_RESEARCH=on with an empty TRET_EGRESS_RESEARCH_ALLOW_HOSTS: the "
            "agent may fetch any public URL, including URLs it read out of an uploaded "
            "document. Set an allowlist, or put an egress proxy in front (TRET_EGRESS_PROXY) "
            "— docs/hardening.md §9."
        )
    if s.egress_research != "off" and not (s.search_provider or "").strip():
        warnings.append(
            "TRET_EGRESS_RESEARCH is enabled but TRET_SEARCH_PROVIDER is empty: fetch_url "
            "works, web_search will tell the model it is unconfigured."
        )
    if not s.cookie_secure:
        warnings.append(
            "TRET_COOKIE_SECURE is false in production: the session cookie will be sent over "
            "plain HTTP. Terminate TLS in front of tret and set TRET_COOKIE_SECURE=true."
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
            "Refusing to start with TRET_ENVIRONMENT=production:\n  - "
            + "\n  - ".join(fatal)
            + "\nSee docs/hardening.md."
        )

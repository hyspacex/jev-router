"""Configuration models for router.yaml.

Everything the router can be taught without touching code lives here:
providers, models, Jev questions, named routes, routing rules, aliases,
per-client floors, the circuit breaker and the quota sources.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    model_validator,
)

# Ordered weakest to strongest. Used for caps, floors and clamping.
DEFAULT_EFFORT_ORDER = ["none", "low", "medium", "high", "xhigh", "max"]

QUESTION_TYPES = {"choice", "score", "noul"}

# Set this to point the router at a proxy without editing router.yaml.
UPSTREAM_ENV = "JEV_ROUTER_UPSTREAM"

# The provider a model belongs to when router.yaml declares no `providers:`
# block at all. Configs written before providers existed keep working: they
# behave as one provider with no quota source and its own breaker.
DEFAULT_PROVIDER = "default"

# Keys that may sit beside a comparison inside a rule condition. They do not
# compare anything; they say how pressure moves the comparison.
SHIFT_KEYS = {"shift_with", "max_shift"}


class ConfigError(Exception):
    """Raised with a human readable message when router.yaml is wrong."""


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Route(Base):
    """What a rule chooses: one model, or the name of a route.

    `{model: x, effort: y}` is the original form and still works. `{route: n}`
    names a lane in the top-level `routes:` map, which carries a primary and an
    ordered list of fallbacks.
    """

    model: str | None = None
    effort: str | None = None
    route: str | None = None

    @model_validator(mode="after")
    def _one_of(self) -> Route:
        if not self.model and not self.route:
            raise ValueError("give a 'model' or a 'route'")
        if self.model and self.route:
            # An overlay that names a concrete model is saying "this one,
            # whatever the file underneath said". Deep-merging a `{model,
            # effort}` block over a `{route}` block is how the eval variants
            # override the shipped policy, so it has to mean something rather
            # than fail.
            self.route = None
        if self.route and self.effort:
            raise ValueError("'effort' belongs to the route's entries, not beside 'route'")
        return self


class RouteEntry(Base):
    """One rung of a route: a model, an effort, and whether it may be promoted.

    `equivalent: true` says this entry is as good as the primary for the work
    the route takes, so quota pressure may move it ahead of the primary. A
    weaker fallback leaves this false and can only ever be reached by failure.
    """

    model: str
    effort: str | None = None
    equivalent: bool = False


class Lane(Base):
    """A named route: one primary and an ordered list of fallbacks."""

    primary: RouteEntry
    fallbacks: list[RouteEntry] = Field(default_factory=list)
    description: str = ""

    def entries(self) -> list[RouteEntry]:
        return [self.primary, *self.fallbacks]


class BreakerCfg(Base):
    """Per-provider circuit breaker. State lives in memory, never on disk."""

    failures_to_open: int = 3
    cooldown_seconds: float = 120.0
    # The cooldown doubles on every reopen, up to this.
    max_cooldown_seconds: float = 1920.0
    # How long a provider's breaker-derived pressure takes to decay to zero
    # after it recovers.
    recovery_seconds: float = 120.0


class QuotaSourceCfg(Base):
    """Where one provider's usage numbers come from.

    `enabled: false` is the shipped default, so a fresh install never runs a
    subprocess or reaches for anything outside the router.
    """

    source: str = "none"
    enabled: bool = False
    poll_seconds: float = 300.0
    stale_after_seconds: float = 1800.0
    # Passed to the source verbatim. Its shape is the source's business.
    config: dict[str, Any] = Field(default_factory=dict)


class ProviderCfg(Base):
    """One upstream account or subscription that several models share."""

    description: str = ""
    breaker: BreakerCfg | None = None
    quota: QuotaSourceCfg | None = None


class QuotaPolicyCfg(Base):
    """How quota pressure is computed and what it is allowed to do."""

    enabled: bool = True
    # --- turning a snapshot into a number in [0, 1] ---
    # How far ahead of pace, in percentage points, before pressure begins.
    pressure_starts_at: float = 0.0
    # How far ahead of pace means pressure 1.0.
    pressure_full_at: float = 25.0
    # Used percent at or above which pressure is 1.0 whatever the pace says.
    full_used_percent: float = 90.0
    # Used only when neither an expected percent nor a window is available.
    raw_starts_at: float = 60.0
    raw_full_at: float = 95.0
    # A new value nearer than this to the last one is ignored, so pressure
    # does not flap around a knee.
    hysteresis: float = 0.05
    # The most pressure may move in one poll.
    max_change_per_poll: float = 0.25
    # --- what pressure does ---
    # A route entry whose provider is under more pressure than this moves
    # behind the healthy entries of the same route.
    demote_above: float = 0.6


class Settings(Base):
    host: str = "127.0.0.1"
    port: int = 8318
    upstream_base_url: str = "http://127.0.0.1:8317"
    jev_url: str = "https://api.typesafe.ai/v1/systemone"
    jev_model: str = "jev-latest"
    jev_timeout_ms: int = 1500
    jev_api_key_env: str = "TYPESAFE_API_KEY"
    pin_ttl_seconds: int = 6 * 60 * 60
    mode: Literal["shadow", "active"] = "active"
    sqlite_path: str = "~/.jev-router/router.db"
    default_route: Route
    effort_order: list[str] = Field(default_factory=lambda: list(DEFAULT_EFFORT_ORDER))
    log_state: bool = False
    decider: str = "jev"
    fallback_decider: str | None = "rules"
    upstream_connect_timeout_s: float = 10.0
    upstream_read_timeout_s: float = 900.0
    capture_usage_max_bytes: int = Field(default=1_048_576, ge=0)
    max_body_bytes: int = Field(default=16 * 1024 * 1024, gt=0)
    max_concurrent_requests: int = Field(default=64, gt=0)
    # Router-owned endpoints only; never forwarded to the model API.
    admin_token_env: str = "JEV_ROUTER_ADMIN_TOKEN"  # noqa: S105 - env var name, not a secret
    # The default breaker for every provider that does not override it.
    breaker: BreakerCfg = Field(default_factory=BreakerCfg)

    @property
    def db_path(self) -> Path:
        return Path(os.path.expanduser(self.sqlite_path))

    @model_validator(mode="after")
    def _upstream_from_env(self) -> Settings:
        """$JEV_ROUTER_UPSTREAM wins over the value in router.yaml."""
        override = os.environ.get(UPSTREAM_ENV, "").strip()
        if override:
            self.upstream_base_url = override
        return self


class ModelCfg(Base):
    upstream_id: str
    provider: str = ""
    context_window: int = 128_000
    supports_tools: bool = True
    supports_vision: bool = False
    efforts: list[str] = Field(default_factory=list)
    # Some models think without limit when they are sent bare. Naming a
    # default_effort here means no code path can send this model without one.
    default_effort: str | None = None
    effort_style: Literal["suffix", "param", "none"] = "none"
    tags: list[str] = Field(default_factory=list)
    description: str = ""


class Rule(Base):
    name: str
    when: dict[str, Any] = Field(default_factory=dict)
    use: Route
    # A protected rule ignores quota threshold shifts. It is for the moves that
    # exist because the request needs them: harm, vision, context overflow.
    protected: bool = False


class LowConfidence(Base):
    """Fires before the rules when a gated answer is not confident enough."""

    min_confidence: float = 0.0
    questions: list[str] = Field(default_factory=list)
    use: Route | None = None


class Ruleset(Base):
    rules: list[Rule] = Field(default_factory=list)
    default: Route | None = None
    low_confidence: LowConfidence | None = None


class DraftCfg(Base):
    """An optional cheap draft shown to Jev before it decides.

    AutoMix (arXiv 2310.12963) asks a cheap model to answer first and then asks
    whether that answer will do. This is the same idea with the second step
    done by the decision model rather than by a prompted LLM. It is off by
    default: it costs one extra upstream call and about a second of latency on
    the first turn of a conversation, and it only pays for itself if the
    question that reads the draft changes enough routes.

    It never runs on a pinned turn, because a pinned conversation does not call
    the decider at all. Any failure is swallowed and the router decides without
    the draft.
    """

    enabled: bool = False
    model: str = ""
    # The effort the draft runs at. It must be spelled the way the model
    # expects, because a reasoning model given no effort and a small token
    # budget spends the whole budget thinking and returns empty content.
    # That is how the first measured draft run failed.
    effort: str | None = "none"
    max_tokens: int = 150
    timeout_ms: int = 4000
    # The env var holding the key the upstream wants. Empty means no header,
    # which is right for a local proxy that does not check one.
    api_key_env: str = ""
    state_field: str = "draft_answer_from_a_fast_model"


class AliasCfg(Base):
    state_builder: str = "summary_v1"
    questions: list[str] = Field(default_factory=list)
    # Either the name of a ruleset ("policy" or a key of `rulesets`) or an inline one.
    rules: str | Ruleset = "policy"
    allowed_models: list[str] | None = None
    max_effort: str | None = None
    decider: str | None = None
    draft: DraftCfg | None = None
    description: str = ""


class ClientCfg(Base):
    """Floors for one value of the X-Router-Client header."""

    min_effort: str | None = None
    allowed_models: list[str] | None = None
    description: str = ""


class RouterConfig(Base):
    settings: Settings
    providers: dict[str, ProviderCfg] = Field(default_factory=dict)
    models: dict[str, ModelCfg]
    questions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    routes: dict[str, Lane] = Field(default_factory=dict)
    policy: Ruleset = Field(default_factory=Ruleset)
    rulesets: dict[str, Ruleset] = Field(default_factory=dict)
    aliases: dict[str, AliasCfg] = Field(default_factory=dict)
    clients: dict[str, ClientCfg] = Field(default_factory=dict)
    quota_policy: QuotaPolicyCfg = Field(default_factory=QuotaPolicyCfg)

    # Set by load_config; stored with every decision so a later tuning run
    # knows which router.yaml produced it.
    _config_hash: str = PrivateAttr(default="")
    _config_path: str = PrivateAttr(default="")

    @property
    def config_hash(self) -> str:
        return self._config_hash

    @property
    def config_path(self) -> str:
        return self._config_path

    # --- lookups -------------------------------------------------------

    def ruleset_for(self, alias: AliasCfg) -> Ruleset:
        if isinstance(alias.rules, Ruleset):
            return alias.rules
        if alias.rules == "policy":
            return self.policy
        return self.rulesets[alias.rules]

    def question(self, qid: str) -> dict[str, Any]:
        return self.questions[qid]

    def is_alias(self, name: str) -> bool:
        return name in self.aliases

    def provider_of(self, model_id: str) -> str:
        mcfg = self.models.get(model_id)
        if mcfg is not None and mcfg.provider:
            return mcfg.provider
        return DEFAULT_PROVIDER

    def provider_names(self) -> list[str]:
        """Every provider a model refers to, declared or implicit."""
        names = list(self.providers)
        for mid in self.models:
            name = self.provider_of(mid)
            if name not in names:
                names.append(name)
        return names

    def breaker_cfg(self, provider: str) -> BreakerCfg:
        pcfg = self.providers.get(provider)
        if pcfg is not None and pcfg.breaker is not None:
            return pcfg.breaker
        return self.settings.breaker

    def quota_cfg(self, provider: str) -> QuotaSourceCfg | None:
        pcfg = self.providers.get(provider)
        return pcfg.quota if pcfg is not None else None

    # --- cross-field validation ----------------------------------------

    @model_validator(mode="after")
    def _check_references(self) -> RouterConfig:
        problems: list[str] = []
        order = self.settings.effort_order
        model_ids = set(self.models)

        def check_pair(model: str, effort: str | None, where: str) -> None:
            if model not in model_ids:
                problems.append(f"{where}: unknown model {model!r}")
                return
            if effort is None:
                return
            if effort not in order:
                problems.append(
                    f"{where}: effort {effort!r} is not in settings.effort_order"
                )
            allowed = self.models[model].efforts
            if allowed and effort not in allowed:
                problems.append(
                    f"{where}: model {model!r} does not allow effort "
                    f"{effort!r} (allowed: {', '.join(allowed) or 'none'})"
                )

        def check_route(route: Route | None, where: str) -> None:
            if route is None:
                return
            if route.route is not None:
                if route.route not in self.routes:
                    problems.append(
                        f"{where}: unknown route {route.route!r} "
                        f"(known: {', '.join(sorted(self.routes)) or 'none'})"
                    )
                return
            check_pair(route.model or "", route.effort, where)

        for name, lane in self.routes.items():
            for i, entry in enumerate(lane.entries()):
                spot = "primary" if i == 0 else f"fallbacks[{i - 1}]"
                check_pair(entry.model, entry.effort, f"routes.{name}.{spot}")
            if lane.primary.equivalent:
                problems.append(
                    f"routes.{name}.primary: 'equivalent' belongs on a fallback, "
                    "not on the primary"
                )

        declared_providers = set(self.providers)
        for mid, mcfg in self.models.items():
            for eff in mcfg.efforts:
                if eff not in order:
                    problems.append(
                        f"models.{mid}.efforts: {eff!r} is not in settings.effort_order"
                    )
            if mcfg.efforts and mcfg.effort_style == "none":
                problems.append(
                    f"models.{mid}: efforts are listed but effort_style is 'none'"
                )
            if mcfg.default_effort is not None:
                if mcfg.default_effort not in order:
                    problems.append(
                        f"models.{mid}.default_effort: {mcfg.default_effort!r} is not in "
                        "settings.effort_order"
                    )
                elif mcfg.efforts and mcfg.default_effort not in mcfg.efforts:
                    problems.append(
                        f"models.{mid}.default_effort: {mcfg.default_effort!r} is not in "
                        f"models.{mid}.efforts ({', '.join(mcfg.efforts)})"
                    )
                elif not mcfg.efforts:
                    problems.append(
                        f"models.{mid}.default_effort is set but the model lists no efforts"
                    )
            if declared_providers:
                if not mcfg.provider:
                    problems.append(
                        f"models.{mid}: 'provider' is required once providers are declared "
                        f"(known: {', '.join(sorted(declared_providers))})"
                    )
                elif mcfg.provider not in declared_providers:
                    problems.append(
                        f"models.{mid}.provider: unknown provider {mcfg.provider!r} "
                        f"(known: {', '.join(sorted(declared_providers))})"
                    )
            elif mcfg.provider:
                problems.append(
                    f"models.{mid}.provider: {mcfg.provider!r} is named but router.yaml "
                    "declares no `providers:` block"
                )

        for qid, q in self.questions.items():
            qtype = q.get("type")
            if qtype not in QUESTION_TYPES:
                problems.append(
                    f"questions.{qid}: type must be one of {sorted(QUESTION_TYPES)}, got {qtype!r}"
                )
                continue
            if not q.get("instructions"):
                problems.append(f"questions.{qid}: 'instructions' is required")
            if qtype == "choice" and not isinstance(q.get("criteria"), dict):
                problems.append(f"questions.{qid}: choice needs a criteria mapping")
            if qtype == "score":
                crit = q.get("criteria")
                if not isinstance(crit, list) or not 2 <= len(crit) <= 10:
                    problems.append(
                        f"questions.{qid}: score needs a criteria list of 2 to 10 levels"
                    )

        check_route(self.settings.default_route, "settings.default_route")

        named_rulesets = {"policy": self.policy, **self.rulesets}
        for name, rs in named_rulesets.items():
            where = "policy" if name == "policy" else f"rulesets.{name}"
            check_route(rs.default, f"{where}.default")
            if rs.low_confidence:
                check_route(rs.low_confidence.use, f"{where}.low_confidence.use")
                for qid in rs.low_confidence.questions:
                    if qid not in self.questions:
                        problems.append(
                            f"{where}.low_confidence: unknown question {qid!r}"
                        )
            for rule in rs.rules:
                check_route(rule.use, f"{where}.rules[{rule.name}].use")
                problems.extend(self._check_when(rule.when, f"{where}.rules[{rule.name}].when"))

        for alias, acfg in self.aliases.items():
            for qid in acfg.questions:
                if qid not in self.questions:
                    problems.append(f"aliases.{alias}.questions: unknown question {qid!r}")
            if isinstance(acfg.rules, str) and acfg.rules not in named_rulesets:
                problems.append(
                    f"aliases.{alias}.rules: unknown ruleset {acfg.rules!r} "
                    f"(known: {', '.join(sorted(named_rulesets))})"
                )
            if isinstance(acfg.rules, Ruleset):
                check_route(acfg.rules.default, f"aliases.{alias}.rules.default")
                for rule in acfg.rules.rules:
                    check_route(rule.use, f"aliases.{alias}.rules[{rule.name}].use")
                    problems.extend(
                        self._check_when(rule.when, f"aliases.{alias}.rules[{rule.name}].when")
                    )
            for mid in acfg.allowed_models or []:
                if mid not in model_ids:
                    problems.append(f"aliases.{alias}.allowed_models: unknown model {mid!r}")
            if acfg.max_effort and acfg.max_effort not in order:
                problems.append(
                    f"aliases.{alias}.max_effort: {acfg.max_effort!r} is not in settings.effort_order"
                )

        for cid, ccfg in self.clients.items():
            if ccfg.min_effort and ccfg.min_effort not in order:
                problems.append(
                    f"clients.{cid}.min_effort: {ccfg.min_effort!r} is not in settings.effort_order"
                )
            for mid in ccfg.allowed_models or []:
                if mid not in model_ids:
                    problems.append(f"clients.{cid}.allowed_models: unknown model {mid!r}")

        if problems:
            raise ValueError("\n".join(f"- {p}" for p in problems))
        return self

    def _check_when(self, when: dict[str, Any], where: str) -> list[str]:
        from .policy import FEATURE_CONDITIONS  # local import avoids a cycle

        problems: list[str] = []
        known_providers = set(self.provider_names())
        for key, cond in when.items():
            if isinstance(cond, dict) and set(cond) & SHIFT_KEYS:
                problems.extend(self._check_shift(cond, known_providers, f"{where}.{key}"))
            if key in FEATURE_CONDITIONS:
                continue
            if key not in self.questions:
                known = sorted(set(self.questions) | set(FEATURE_CONDITIONS))
                problems.append(
                    f"{where}: {key!r} is neither a question id nor a request feature "
                    f"(known: {', '.join(known)})"
                )
                continue
            if not isinstance(cond, dict):
                problems.append(f"{where}.{key}: expected a mapping of comparisons")
        return problems

    def _check_shift(
        self, cond: dict[str, Any], known_providers: set[str], where: str
    ) -> list[str]:
        problems: list[str] = []
        provider = cond.get("shift_with")
        if provider is None:
            problems.append(f"{where}: 'max_shift' needs a 'shift_with' provider beside it")
        elif provider not in known_providers:
            problems.append(
                f"{where}.shift_with: unknown provider {provider!r} "
                f"(known: {', '.join(sorted(known_providers))})"
            )
        amount = cond.get("max_shift")
        if amount is None:
            problems.append(f"{where}: 'shift_with' needs a 'max_shift' beside it")
        elif not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
            problems.append(f"{where}.max_shift: expected a number of zero or more")
        if not set(cond) & {"gte", "lte", "conf_gte"}:
            problems.append(
                f"{where}: a shift needs a 'gte', 'lte' or 'conf_gte' to move"
            )
        return problems


def load_config(path: str | Path, check_registries: bool = True) -> RouterConfig:
    """Read and validate router.yaml, raising ConfigError with readable text."""
    path = Path(os.path.expanduser(str(path)))
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    text = path.read_text()
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    try:
        cfg = RouterConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path} is invalid:\n{_format_errors(exc)}") from exc
    cfg._config_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
    cfg._config_path = str(path)

    if check_registries:
        problems = check_registry_names(cfg)
        if problems:
            raise ConfigError(
                f"{path} is invalid:\n" + "\n".join(f"- {p}" for p in problems)
            )
    return cfg


def check_registry_names(cfg: RouterConfig) -> list[str]:
    """Check names that resolve against code registries (builders, deciders)."""
    from .deciders import DECIDERS
    from .quota import QUOTA_SOURCES
    from .state import STATE_BUILDERS

    problems: list[str] = []
    for name, pcfg in cfg.providers.items():
        if pcfg.quota and pcfg.quota.source not in QUOTA_SOURCES:
            problems.append(
                f"providers.{name}.quota.source: unknown source {pcfg.quota.source!r} "
                f"(known: {', '.join(sorted(QUOTA_SOURCES))})"
            )
    for alias, acfg in cfg.aliases.items():
        if acfg.state_builder not in STATE_BUILDERS:
            problems.append(
                f"aliases.{alias}.state_builder: unknown builder {acfg.state_builder!r} "
                f"(known: {', '.join(sorted(STATE_BUILDERS))})"
            )
        if acfg.decider and acfg.decider not in DECIDERS:
            problems.append(
                f"aliases.{alias}.decider: unknown decider {acfg.decider!r} "
                f"(known: {', '.join(sorted(DECIDERS))})"
            )
    if cfg.settings.decider not in DECIDERS:
        problems.append(
            f"settings.decider: unknown decider {cfg.settings.decider!r} "
            f"(known: {', '.join(sorted(DECIDERS))})"
        )
    if cfg.settings.fallback_decider and cfg.settings.fallback_decider not in DECIDERS:
        problems.append(
            f"settings.fallback_decider: unknown decider {cfg.settings.fallback_decider!r}"
        )
    return problems


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        msg = err["msg"]
        if loc == "(root)" and "\n" in msg:
            lines.append(msg.replace("Value error, ", ""))
        else:
            lines.append(f"- {loc}: {msg}")
    return "\n".join(lines)

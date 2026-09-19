"""Configuration models for router.yaml.

Everything the router can be taught without touching code lives here: models,
Jev questions, routing rules, aliases and per-client floors.
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


class ConfigError(Exception):
    """Raised with a human readable message when router.yaml is wrong."""


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Route(Base):
    """A model plus an optional reasoning effort."""

    model: str
    effort: str | None = None


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
    capture_usage_max_bytes: int = 1_048_576

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
    context_window: int = 128_000
    supports_tools: bool = True
    supports_vision: bool = False
    efforts: list[str] = Field(default_factory=list)
    effort_style: Literal["suffix", "param", "none"] = "none"
    tags: list[str] = Field(default_factory=list)
    description: str = ""


class Rule(Base):
    name: str
    when: dict[str, Any] = Field(default_factory=dict)
    use: Route


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
    models: dict[str, ModelCfg]
    questions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    policy: Ruleset = Field(default_factory=Ruleset)
    rulesets: dict[str, Ruleset] = Field(default_factory=dict)
    aliases: dict[str, AliasCfg] = Field(default_factory=dict)
    clients: dict[str, ClientCfg] = Field(default_factory=dict)

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

    # --- cross-field validation ----------------------------------------

    @model_validator(mode="after")
    def _check_references(self) -> RouterConfig:
        problems: list[str] = []
        order = self.settings.effort_order
        model_ids = set(self.models)

        def check_route(route: Route | None, where: str) -> None:
            if route is None:
                return
            if route.model not in model_ids:
                problems.append(f"{where}: unknown model {route.model!r}")
                return
            if route.effort is None:
                return
            if route.effort not in order:
                problems.append(
                    f"{where}: effort {route.effort!r} is not in settings.effort_order"
                )
            allowed = self.models[route.model].efforts
            if allowed and route.effort not in allowed:
                problems.append(
                    f"{where}: model {route.model!r} does not allow effort "
                    f"{route.effort!r} (allowed: {', '.join(allowed) or 'none'})"
                )

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
        for key, cond in when.items():
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
    from .state import STATE_BUILDERS

    problems: list[str] = []
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

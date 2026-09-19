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
    field_validator,
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


class EffortControlCfg(Base):
    """What this deployment is allowed to do to effort between turns.

    `fixed` is the only setting that does anything today. The others record
    what a later experiment would have to be qualified for, and `verified`
    means somebody ran a dated deployment test and wrote down where it is.
    A model name alone is never the qualification: the endpoint, the proxy in
    front of it and the adapter revision are part of what was tested.
    """

    between_turn: Literal[
        "fixed", "native_configuration_update", "request_parameter"
    ] = "fixed"
    qualification: Literal["unverified", "verified"] = "unverified"
    cache_behavior: Literal[
        "verified_preserving", "verified_breaking", "unverified"
    ] = "unverified"
    qualification_ref: str | None = None

    @model_validator(mode="after")
    def _verified_needs_a_report(self) -> EffortControlCfg:
        if self.qualification == "verified" and not (self.qualification_ref or "").strip():
            raise ValueError(
                "qualification 'verified' needs a 'qualification_ref' naming the dated "
                "deployment test report"
            )
        return self


class EffortThresholds(Base):
    """The numbers the between-turn effort policy compares against.

    All of them are engineering starting points, not measured findings. They
    are recorded on every plan so a replay can see what a decision was taken
    under, and `evals/effort_replay.py` searches them offline.
    """

    # What makes the policy want more effort. Any one of these is enough.
    upgrade_difficulty: float = 1.8
    upgrade_harm: float = 0.6
    upgrade_p_hard: float = 0.4
    # A follow-up that names a defect in the earlier result argues for more.
    corrective_followup: float = 0.6
    # What makes it want less. All of them have to hold at once.
    downgrade_difficulty: float = 0.8
    downgrade_harm: float = 0.2
    # Quota pressure at or above this resolves an otherwise-hold turn downward.
    # It never moves a turn the evidence says is demanding, and never crosses
    # a floor.
    quota_pressure_at: float = Field(default=0.6, ge=0.0, le=1.0)
    # How close to the negotiated window the experiment stops adapting.
    context_safety_fraction: float = Field(default=0.8, gt=0.0, le=1.0)


class EffortTargets(Base):
    """Which rung a turn's evidence asks for, rather than simply "one more".

    Owner decision, 2026-09-19: an upward change goes straight to the rung the
    evidence names, so a demanding turn on `low` may land on `high` in one
    move. This block is that mapping, and `AdaptiveEffortCfg.upward` is the
    switch that restores the old one-rung climb.

    The names here are rungs of a ladder. A rung a profile does not offer is
    ignored rather than being an error, because one mapping is shared by every
    ladder and a profile may offer only two rungs. `"top"` means the strongest
    rung the session is allowed, whatever it is called.
    """

    # The difficulty at or above which each rung becomes the target, on the
    # 0-3 score scale Jev answers a Score question on. The live run of
    # 2026-09-19 saw 2.95 on a turn a person would call clearly hard, and 1.9
    # on a moderately hard one, so the defaults put those two turns on
    # different rungs.
    difficulty: dict[str, float] = Field(
        default_factory=lambda: {"medium": 1.8, "high": 2.6}
    )
    # Difficulty mass sitting on the top level. At or above this the turn asks
    # for the top rung outright, whatever the mean difficulty says.
    p_hard_top: float = Field(default=0.6, ge=0.0, le=1.0)
    # What a follow-up that names a defect in the earlier result asks for. A
    # concise corrective message may need a great deal of effort (spec 10.5).
    corrective_rung: str = "top"
    # What a turn asks for when a protected admission rule bound this session
    # and the evidence asks for more at all. Protected work exists because
    # somebody said it must not be served weakly.
    protected_rung: str = "top"


class AdaptiveEffortCfg(Base):
    """The between-turn effort experiment (spec 10). Off unless asked for.

    `off` is the default and an omitted `experiments:` block means `off`,
    which is exactly how the router behaved before any of this existed: no
    per-turn classifier call and no effort ever moves.

    `shadow` computes a recommendation at an eligible boundary, records it,
    and sends the fixed-effort request unchanged. `active` applies the change,
    and only on a deployment somebody qualified: config load refuses it unless
    every listed profile declares a tested between-turn strategy, carries
    `qualification: verified`, and names a report file that exists.

    Both an alias (`adaptive_effort: true`) and the client contract
    (`turn_boundary_reporting: true` at resolve) have to opt in. A session
    resolved before either was true never gains the feature: the mode it was
    resolved under is stored on the session row.
    """

    mode: Literal["off", "shadow", "active"] = "off"

    @field_validator("mode", mode="before")
    @classmethod
    def _yaml_reads_off_as_false(cls, value: Any) -> Any:
        """`mode: off` in YAML is the boolean false. Take it as the word."""
        if value is False:
            return "off"
        if value is True:
            raise ValueError("mode must be 'off', 'shadow' or 'active'; quote it")
        return value

    # Exact model keys. A profile not listed here never adapts.
    qualified_profiles: list[str] = Field(default_factory=list)
    evaluate_on: Literal["new_user_turn"] = "new_user_turn"
    # Consecutive eligible-turn recommendations a downgrade needs.
    downgrade_confirmations: int = Field(default=2, ge=1)
    # What a Jev timeout, malformed output or missing evidence does.
    on_unknown: Literal["keep"] = "keep"
    # False compares the same signals with no confirmation counting, which is
    # the simpler policy replay measures this one against.
    hysteresis: bool = True
    # The ladder the policy may move along, weakest first. One global default
    # and an optional per-profile override.
    ladder: list[str] = Field(default_factory=lambda: ["low", "medium", "high"])
    ladders: dict[str, list[str]] = Field(default_factory=dict)
    thresholds: EffortThresholds = Field(default_factory=EffortThresholds)
    # How far one upward change may move. `jump` goes to the rung the evidence
    # asks for, which is the owner's decision of 2026-09-19; `step` restores
    # the single rung the policy used to climb.
    upward: Literal["jump", "step"] = "jump"
    # How far one confirmed downward change may move. `step` is one rung and
    # is the default on purpose: a downgrade is the change that can cost
    # quality, so each further rung is argued and confirmed again on its own
    # evidence. `jump` drops straight to the floor.
    downward: Literal["step", "jump"] = "step"
    targets: EffortTargets = Field(default_factory=EffortTargets)
    # What is asked at a turn boundary. These are turn questions, not the
    # alias's admission questions.
    questions: list[str] = Field(
        default_factory=lambda: ["difficulty", "harm_if_wrong", "corrective_followup"]
    )
    # Asked in the same call, recorded, and never read by the policy. Only
    # sent when the turn carries a grounded harness observation.
    shadow_questions: list[str] = Field(default_factory=lambda: ["failure_mode"])

    def ladder_for(self, model_key: str) -> list[str]:
        return list(self.ladders.get(model_key) or self.ladder)


class ExperimentsCfg(Base):
    """Everything behind an experimental gate. Each one defaults to off."""

    adaptive_effort: AdaptiveEffortCfg = Field(default_factory=AdaptiveEffortCfg)


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
    # --- execution profile, used by strict sessions ---------------------
    # The wire protocol this deployment actually speaks. Configure what the
    # endpoint does, not what the model family supports elsewhere.
    protocol: Literal["openai-chat", "openai-responses"] = "openai-chat"
    # An operational cap on one reply, not a claim about the model's maximum.
    max_output_tokens: int | None = Field(default=None, gt=0)
    # Names the deployment this row was checked against: model, endpoint and
    # adapter revision together.
    compatibility_revision: str = ""
    effort_control: EffortControlCfg = Field(default_factory=EffortControlCfg)


class Rule(Base):
    name: str
    when: dict[str, Any] = Field(default_factory=dict)
    use: Route
    # A protected rule ignores quota threshold shifts. It is for the moves that
    # exist because the request needs them: harm, vision, context overflow.
    protected: bool = False
    # The quality lane this rule's work belongs to. Naming one says which set
    # of model/effort pairs has been measured for it, and quota pressure may
    # then only move the request inside that set.
    lane: str = ""


class LowConfidence(Base):
    """Fires before the rules when a gated answer is not confident enough.

    `action_equivalent` is opt-in and off by default, so the gate behaves
    exactly as it always has unless a config asks for more. Turned on, the gate
    first checks whether the labels the answer is torn between would all route
    the same way; being unsure between two labels that lead to the same model
    and effort is not a reason to escalate. It needs the answer's probability
    vector, so with a provider that sends none it also changes nothing.
    """

    min_confidence: float = 0.0
    questions: list[str] = Field(default_factory=list)
    use: Route | None = None
    action_equivalent: bool = False
    # How much mass a label needs before it counts as one the answer is torn
    # between. Below this it is noise, not a second reading.
    equivalence_min_mass: float = Field(default=0.1, ge=0.0, le=1.0)


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


class StaticEnvelopeCfg(Base):
    """One static model descriptor over a homogeneous pool (spec 6.3).

    For a client that cannot read resolved metadata. The envelope it may
    advertise is the intersection of the pool: one wire protocol, the smallest
    context window, the smallest output ceiling, and a capability only if
    every model in the pool has it. Declaring more than the pool can keep is a
    config error, because the client would size its history against a window
    the chosen model does not have.
    """

    enabled: bool = True
    # The name the client is registered with and may send as `model`. It is
    # accepted only because this block declares it. Empty means the alias name.
    model_name: str = ""
    # Advertise less than the pool minimum if you like; never more.
    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)


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
    # `legacy` is every alias that existed before strict sessions: a TTL pin
    # the router may replace. `strict` is a durable binding that is resolved
    # once and never moved. An omitted field means legacy.
    session_mode: Literal["legacy", "strict"] = "legacy"
    static_envelope: StaticEnvelopeCfg | None = None
    # Opt in to the between-turn effort experiment. This alone does nothing:
    # the experiment also has to be on in `experiments.adaptive_effort`, the
    # client has to declare `turn_boundary_reporting`, and the bound profile
    # has to be a qualified one.
    adaptive_effort: bool = False
    # What a strict admission may use when Jev is down (spec 8.4). Without one
    # the admission returns NO_SAFE_ADMISSION rather than guessing, because a
    # strict binding taken under a fallback is as fixed as any other.
    admission_fallback: Route | None = None


class ClientCfg(Base):
    """Floors for one value of the X-Router-Client header."""

    min_effort: str | None = None
    allowed_models: list[str] | None = None
    description: str = ""


class SessionRoutingCfg(Base):
    """The strict session service. Off unless router.yaml turns it on."""

    enabled: bool = False
    # How long a resolved but never executed binding stays usable.
    prepared_ttl_seconds: int = Field(default=600, gt=0)
    # One execution per session at a time. A second concurrent one is a
    # conflict, not a queue.
    max_inflight_per_session: int = Field(default=1, ge=1)
    # Strict control and execution requests need the router credential, always
    # (spec 9.1), where the read-only endpoints accept a loopback peer. The
    # field stays so a config that already says `true` keeps loading, and
    # `false` is refused rather than quietly turning the check off: a strict
    # session is scoped to the owner of that credential, so there has to be
    # one to scope it to.
    require_control_token: bool = True

    @field_validator("require_control_token")
    @classmethod
    def _always_required(cls, value: bool) -> bool:
        if not value:
            raise ValueError(
                "strict control and execution requests always need the router "
                "control credential, even on loopback: a strict session is "
                "scoped to its owner. Remove 'require_control_token: false', or "
                "set session_routing.enabled: false if this deployment does not "
                "want strict sessions at all"
            )
        return value
    # How many finished request-ID records one session keeps. Records whose
    # outcome is unknown are never dropped, so a forgotten ID is never read as
    # proof that it did not execute.
    max_request_history: int = Field(default=200, gt=0)


class SemanticPolicyCfg(Base):
    """Which questions decide, which are only measured, and how far.

    `active_questions` is the set an alias may route on. `shadow_questions` are
    asked in the same batched call, validated on their own, recorded, and never
    read by the routing path. The two sets may not overlap: a question cannot
    be both the answer and the experiment.

    `distribution_policy` says what a rule condition over a Score's probability
    vector may do. `off` is the default, and an omitted `semantic_policy` block
    means `off`, which is exactly how the router behaved before any of this
    existed. `shadow` computes the experimental route beside the real one and
    records both. `active` lets it choose.
    """

    active_questions: list[str] = Field(default_factory=list)
    shadow_questions: list[str] = Field(default_factory=list)
    distribution_policy: Literal["off", "shadow", "active"] = "off"


class LaneModel(Base):
    """One model/effort pair somebody measured for one lane of work."""

    model: str
    effort: str | None = None
    # Where the evidence is: a POOL.md heading, an admission artifact, a dated
    # report. A pair with no reference is a guess, so it is refused.
    qualification_ref: str

    def as_pair(self) -> tuple[str, str | None]:
        return (self.model, self.effort)


class QualityLaneCfg(Base):
    """A class of work with qualification evidence behind each candidate.

    Capability and adequacy are different filters. A model that supports tools
    is not thereby qualified to finish a tool-driven coding task, so quota
    pressure may only move a request between the pairs listed here.
    """

    description: str = ""
    # Where the lane as a whole was measured.
    qualification_ref: str = ""
    qualified: list[LaneModel] = Field(default_factory=list)
    # What this lane takes when the evidence runs out: the safe end, not the
    # cheap end.
    conservative_default: Route | None = None

    def pairs(self) -> list[tuple[str, str | None]]:
        return [q.as_pair() for q in self.qualified]

    def models(self) -> list[str]:
        return [q.model for q in self.qualified]

    def allows(self, model: str, effort: str | None) -> bool:
        """Is this exact pair qualified? An unlisted effort is not qualified.

        A lane entry with no effort means "any effort of this model", which is
        how a model with no effort ladder is written down.
        """
        for entry in self.qualified:
            if entry.model != model:
                continue
            if entry.effort is None or entry.effort == effort:
                return True
        return False

    def reference_for(self, model: str, effort: str | None) -> str:
        for entry in self.qualified:
            if entry.model == model and entry.effort in (None, effort):
                return entry.qualification_ref
        return self.qualification_ref


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
    session_routing: SessionRoutingCfg = Field(default_factory=SessionRoutingCfg)
    semantic_policy: SemanticPolicyCfg = Field(default_factory=SemanticPolicyCfg)
    quality_lanes: dict[str, QualityLaneCfg] = Field(default_factory=dict)
    experiments: ExperimentsCfg = Field(default_factory=ExperimentsCfg)

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

    def shadow_questions(self, alias_cfg: AliasCfg) -> list[str]:
        """The questions this alias asks only to measure them.

        A shadow question an alias already asks actively is dropped here: it is
        answering for real, and asking it twice in one packet would be a second
        copy of the same judgment.
        """
        asking = set(alias_cfg.questions)
        return [
            qid
            for qid in self.semantic_policy.shadow_questions
            if qid in self.questions and qid not in asking
        ]

    def adaptive_effort(self) -> AdaptiveEffortCfg:
        return self.experiments.adaptive_effort

    def adaptation_mode(self, alias_cfg: AliasCfg, turn_boundaries: bool) -> str:
        """The mode a session resolved now would be bound to.

        Three opt-ins have to agree: the experiment itself, the alias, and the
        client's declared ability to report turn boundaries. Any one of them
        missing means `off`, which is what every session before this release
        was resolved under.
        """
        cfg = self.experiments.adaptive_effort
        if cfg.mode == "off" or not alias_cfg.adaptive_effort or not turn_boundaries:
            return "off"
        return cfg.mode

    def effort_ladder(self, model_key: str) -> list[str]:
        """The rungs this profile may move between, weakest first.

        Intersected with what the model declares, so a ladder cannot name an
        effort the deployment does not have.
        """
        mcfg = self.models.get(model_key)
        if mcfg is None:
            return []
        wanted = self.experiments.adaptive_effort.ladder_for(model_key)
        rank = {e: i for i, e in enumerate(self.settings.effort_order)}
        rungs = [e for e in wanted if e in mcfg.efforts]
        return sorted(rungs, key=lambda e: rank.get(e, len(rank)))

    def lane_for(self, rule_name: str, ruleset: Ruleset) -> QualityLaneCfg | None:
        """The lane a rule of this ruleset names, when it names one."""
        for rule in ruleset.rules:
            if rule.name == rule_name:
                return self.quality_lanes.get(rule.lane) if rule.lane else None
        return None

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

    def alias_pool(self, alias_cfg: AliasCfg) -> list[str]:
        """The models an alias may use, in configured order."""
        names = self.models if alias_cfg.allowed_models is None else alias_cfg.allowed_models
        return [m for m in names if m in self.models]

    def envelope_limits(self, alias: str, alias_cfg: AliasCfg) -> dict[str, Any] | None:
        """The static descriptor an envelope alias advertises, or nothing.

        Every number is the minimum over the pool and every capability is the
        intersection, so anything the router later picks fits inside what the
        client was told. Config load already refused a pool that cannot agree
        on one protocol.
        """
        env = alias_cfg.static_envelope
        if env is None or not env.enabled:
            return None
        pool = self.alias_pool(alias_cfg)
        if not pool:
            return None
        rows = [self.models[m] for m in pool]
        outs = [r.max_output_tokens for r in rows]
        floor_output = min(o for o in outs if o is not None) if all(outs) else None
        window = min(r.context_window for r in rows)
        if env.context_window is not None:
            window = min(window, env.context_window)
        if env.max_output_tokens is not None:
            floor_output = (
                env.max_output_tokens
                if floor_output is None
                else min(floor_output, env.max_output_tokens)
            )
        return {
            "model_name": env.model_name or alias,
            "protocol": rows[0].protocol,
            "context_window": window,
            "max_output_tokens": floor_output,
            "supports_tools": all(r.supports_tools for r in rows),
            "supports_vision": all(r.supports_vision for r in rows),
            "pool": pool,
        }

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
                if not rule.lane:
                    continue
                lane_cfg = self.quality_lanes.get(rule.lane)
                if lane_cfg is None:
                    problems.append(
                        f"{where}.rules[{rule.name}].lane: unknown quality lane "
                        f"{rule.lane!r} (known: "
                        f"{', '.join(sorted(self.quality_lanes)) or 'none'})"
                    )
                    continue
                # The primary is the adequacy claim and has to be qualified.
                # The fallbacks below it are for failure, which is a different
                # question, so they are left alone.
                pairs = self.route_pairs(rule.use)
                if pairs and not lane_cfg.allows(*pairs[0]):
                    model, effort = pairs[0]
                    problems.append(
                        f"{where}.rules[{rule.name}]: it routes to "
                        f"{model}({effort or '-'}), which is not in the qualified "
                        f"set of lane {rule.lane!r}"
                    )

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
            check_route(acfg.admission_fallback, f"aliases.{alias}.admission_fallback")
            if acfg.session_mode == "strict" and not self.session_routing.enabled:
                problems.append(
                    f"aliases.{alias}.session_mode: 'strict' needs session_routing.enabled"
                )
            problems.extend(self._check_envelope(alias, acfg))

        problems.extend(self._check_semantic_policy())
        problems.extend(self._check_lanes(check_pair, check_route))
        problems.extend(self._check_adaptive_effort())

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

    def _check_semantic_policy(self) -> list[str]:
        """Every named question exists, and no question is both sets at once."""
        sp = self.semantic_policy
        problems: list[str] = []
        for field_name, ids in (
            ("active_questions", sp.active_questions),
            ("shadow_questions", sp.shadow_questions),
        ):
            for qid in ids:
                if qid not in self.questions:
                    problems.append(
                        f"semantic_policy.{field_name}: unknown question {qid!r} "
                        f"(known: {', '.join(sorted(self.questions)) or 'none'})"
                    )
        both = sorted(set(sp.active_questions) & set(sp.shadow_questions))
        if both:
            problems.append(
                "semantic_policy: "
                f"{', '.join(both)} is in both active_questions and "
                "shadow_questions; a question cannot be the answer and the "
                "experiment at the same time"
            )
        shadow = set(sp.shadow_questions)
        for alias, acfg in self.aliases.items():
            clash = sorted(shadow & set(acfg.questions))
            if clash:
                problems.append(
                    f"aliases.{alias}.questions: {', '.join(clash)} is a shadow "
                    "question; an alias that routes on it must not also be "
                    "measuring it"
                )
        return problems

    def _check_adaptive_effort(self) -> list[str]:
        """What the between-turn experiment needs before it may be switched on.

        `shadow` only has to be well formed: it sends nothing different. For
        `active` every listed profile has to declare a tested between-turn
        strategy and carry a verified qualification, because an effort update
        that the deployment silently ignores or that resets the conversation
        is not something a router can detect after the fact.
        """
        cfg = self.experiments.adaptive_effort
        problems: list[str] = []
        order = self.settings.effort_order
        where = "experiments.adaptive_effort"

        if cfg.mode != "off":
            # The defaults name the questions router.yaml ships for this
            # experiment. A config that never turns it on is not asked to
            # define them.
            for qid in [*cfg.questions, *cfg.shadow_questions]:
                if qid not in self.questions:
                    problems.append(
                        f"{where}: unknown question {qid!r} "
                        f"(known: {', '.join(sorted(self.questions)) or 'none'})"
                    )
        both = sorted(set(cfg.questions) & set(cfg.shadow_questions))
        if both:
            problems.append(
                f"{where}: {', '.join(both)} is both a turn question and a turn "
                "shadow question; a question cannot be the answer and the "
                "experiment at the same time"
            )
        for effort in cfg.ladder:
            if effort not in order:
                problems.append(
                    f"{where}.ladder: {effort!r} is not in settings.effort_order"
                )
        for model, rungs in cfg.ladders.items():
            if model not in self.models:
                problems.append(f"{where}.ladders: unknown model {model!r}")
                continue
            for effort in rungs:
                if effort not in order:
                    problems.append(
                        f"{where}.ladders.{model}: {effort!r} is not in "
                        "settings.effort_order"
                    )
                elif effort not in self.models[model].efforts:
                    problems.append(
                        f"{where}.ladders.{model}: {model!r} does not offer "
                        f"effort {effort!r}"
                    )
        for model in cfg.qualified_profiles:
            if model not in self.models:
                problems.append(
                    f"{where}.qualified_profiles: unknown model {model!r}"
                )

        for alias, acfg in self.aliases.items():
            if not acfg.adaptive_effort:
                continue
            if acfg.session_mode != "strict":
                problems.append(
                    f"aliases.{alias}.adaptive_effort: the between-turn experiment "
                    "needs a strict session; a legacy alias has no turn boundary "
                    "to change effort at"
                )

        if cfg.mode != "active":
            return problems
        if not cfg.qualified_profiles:
            problems.append(
                f"{where}: mode 'active' needs at least one profile in "
                "qualified_profiles; nothing is qualified by default"
            )
        for model in cfg.qualified_profiles:
            mcfg = self.models.get(model)
            if mcfg is None:
                continue
            control = mcfg.effort_control
            spot = f"models.{model}.effort_control"
            if control.between_turn == "fixed":
                problems.append(
                    f"{spot}.between_turn: {model!r} is in "
                    f"{where}.qualified_profiles but declares no between-turn "
                    "strategy; only 'native_configuration_update' or "
                    "'request_parameter' can be activated"
                )
            if control.qualification != "verified":
                problems.append(
                    f"{spot}.qualification: {model!r} is in "
                    f"{where}.qualified_profiles and is still "
                    f"{control.qualification!r}; run the qualification and "
                    "record the report before activating it"
                )
            if (
                control.between_turn == "native_configuration_update"
                and mcfg.effort_style != "param"
            ):
                problems.append(
                    f"models.{model}.effort_style: a native configuration update "
                    "moves the effective effort while the request-level setting "
                    f"stays put, and {mcfg.effort_style!r} encodes the effort in "
                    "the model name; a native profile carries its base effort as "
                    "a request field ('param')"
                )
            if len(self.effort_ladder(model)) < 2:
                problems.append(
                    f"{where}: {model!r} has fewer than two rungs on its effort "
                    "ladder, so there is nothing to move between"
                )
        return problems

    def _check_lanes(self, check_pair: Any, check_route: Any) -> list[str]:
        """A lane's pairs are real, and its conservative default is one of them."""
        problems: list[str] = []
        for name, lane in self.quality_lanes.items():
            where = f"quality_lanes.{name}"
            if not lane.qualified:
                problems.append(f"{where}.qualified: a lane needs at least one pair")
            seen: set[tuple[str, str | None]] = set()
            for i, entry in enumerate(lane.qualified):
                check_pair(entry.model, entry.effort, f"{where}.qualified[{i}]")
                if entry.as_pair() in seen:
                    problems.append(
                        f"{where}.qualified[{i}]: {entry.model} "
                        f"({entry.effort or 'any effort'}) is listed twice"
                    )
                seen.add(entry.as_pair())
            route = lane.conservative_default
            if route is None:
                continue
            check_route(route, f"{where}.conservative_default")
            # Its primary is the adequacy claim. Anything below that is a
            # failure fallback, which says nothing about adequacy.
            pairs = self.route_pairs(route)
            if pairs and not lane.allows(*pairs[0]):
                model, effort = pairs[0]
                problems.append(
                    f"{where}.conservative_default: {model}({effort or '-'}) is not "
                    "in this lane's qualified set"
                )
        return problems

    def route_pairs(self, route: Route) -> list[tuple[str, str | None]]:
        """Every model/effort a `use:` could reach, primary and fallbacks."""
        if route.route is not None:
            lane = self.routes.get(route.route)
            if lane is None:
                return []
            return [(e.model, e.effort) for e in lane.entries()]
        return [(route.model or "", route.effort)]

    def _check_envelope(self, alias: str, acfg: AliasCfg) -> list[str]:
        """A static envelope may only advertise what the whole pool can keep."""
        env = acfg.static_envelope
        if env is None or not env.enabled:
            return []
        where = f"aliases.{alias}.static_envelope"
        pool = self.alias_pool(acfg)
        if not pool:
            return [f"{where}: the alias permits no known model"]
        rows = [self.models[m] for m in pool]
        problems: list[str] = []
        protocols = sorted({r.protocol for r in rows})
        if len(protocols) > 1:
            problems.append(
                f"{where}: one static descriptor needs one wire protocol, but the pool "
                f"mixes {', '.join(protocols)}"
            )
        floor_context = min(r.context_window for r in rows)
        if env.context_window is not None and env.context_window > floor_context:
            problems.append(
                f"{where}.context_window: {env.context_window} is larger than the "
                f"smallest window in the pool ({floor_context})"
            )
        outs = [r.max_output_tokens for r in rows]
        if env.max_output_tokens is not None:
            if not all(outs):
                bare = ", ".join(m for m, r in zip(pool, rows) if r.max_output_tokens is None)
                problems.append(
                    f"{where}.max_output_tokens: every model in the pool needs a "
                    f"max_output_tokens before one can be advertised (missing: {bare})"
                )
            else:
                floor_output = min(o for o in outs if o is not None)
                if env.max_output_tokens > floor_output:
                    problems.append(
                        f"{where}.max_output_tokens: {env.max_output_tokens} is larger "
                        f"than the smallest output ceiling in the pool ({floor_output})"
                    )
        if env.model_name and env.model_name in self.models:
            problems.append(
                f"{where}.model_name: {env.model_name!r} is also a real model id; "
                "give the descriptor its own name"
            )
        return problems

    def _check_when(self, when: dict[str, Any], where: str) -> list[str]:
        from .policy import DISTRIBUTION_OPS, FEATURE_CONDITIONS  # local: avoids a cycle

        problems: list[str] = []
        known_providers = set(self.provider_names())
        shipped = where.startswith("policy.")
        for key, cond in when.items():
            if isinstance(cond, dict) and set(cond) & SHIFT_KEYS:
                problems.extend(self._check_shift(cond, known_providers, f"{where}.{key}"))
            distribution = (
                sorted(set(cond) & DISTRIBUTION_OPS) if isinstance(cond, dict) else []
            )
            if key in FEATURE_CONDITIONS:
                if distribution:
                    problems.append(
                        f"{where}.{key}: {', '.join(distribution)} reads a Jev "
                        "answer's probability vector, so it needs a question id"
                    )
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
                continue
            if not distribution:
                continue
            if self.questions[key].get("type") != "score":
                problems.append(
                    f"{where}.{key}: {', '.join(distribution)} needs a score "
                    f"question, and {key!r} is a "
                    f"{self.questions[key].get('type')!r}"
                )
            if shipped:
                # The shipped ladder is the reproducible baseline. An
                # experiment lives in a named ruleset somebody is measuring,
                # so it can be switched on and off without editing `policy`.
                problems.append(
                    f"{where}.{key}: {', '.join(distribution)} is experimental; "
                    "put the rule in a named ruleset rather than in the shipped "
                    "`policy` block"
                )
            for op in distribution:
                amount = cond[op]
                if (
                    isinstance(amount, bool)
                    or not isinstance(amount, (int, float))
                    or not 0.0 <= amount <= 1.0
                ):
                    problems.append(
                        f"{where}.{key}.{op}: expected a probability from 0 to 1"
                    )
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
    problems.extend(check_qualification_reports(cfg))
    return problems


def check_qualification_reports(cfg: RouterConfig) -> list[str]:
    """An active effort profile has to name a report file that is really there.

    `qualification: verified` already needs a `qualification_ref`, but a
    string is easy to write and hard to check later. Once a profile is
    activated the reference has to resolve to a checked-in file, so "verified"
    always has a dated report behind it that somebody can open.

    Paths resolve against the directory holding router.yaml, then against the
    working directory.
    """
    effort = cfg.experiments.adaptive_effort
    if effort.mode != "active":
        return []
    bases = [Path.cwd()]
    if cfg.config_path:
        bases.insert(0, Path(cfg.config_path).resolve().parent)
    problems: list[str] = []
    for model in effort.qualified_profiles:
        mcfg = cfg.models.get(model)
        if mcfg is None or mcfg.effort_control.qualification != "verified":
            continue
        ref = (mcfg.effort_control.qualification_ref or "").strip()
        # A reference may carry a heading after the path, as the lane
        # references do. The path is the part before the first colon-space.
        name = ref.split(": ", 1)[0].strip()
        if name and any((base / name).is_file() for base in bases):
            continue
        problems.append(
            f"models.{model}.effort_control.qualification_ref: {ref!r} does not "
            "name a file that exists; an active effort profile needs a dated "
            "deployment report checked in beside router.yaml"
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

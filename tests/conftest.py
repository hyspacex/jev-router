from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml

from jev_router.config import UPSTREAM_ENV, RouterConfig, load_config
from jev_router.pins import Store


@pytest.fixture(autouse=True)
def _no_upstream_env(monkeypatch):
    """The env override must not reach into tests that set their own upstream."""
    monkeypatch.delenv(UPSTREAM_ENV, raising=False)

UPSTREAM = "http://upstream.test"

BASE_CONFIG: dict[str, Any] = {
    "settings": {
        "upstream_base_url": UPSTREAM,
        "jev_url": "https://jev.test/v1/systemone",
        "jev_timeout_ms": 500,
        "mode": "active",
        "sqlite_path": ":memory:",
        "decider": "jev",
        "fallback_decider": "rules",
        "default_route": {"model": "big", "effort": "medium"},
    },
    "models": {
        "small": {
            "upstream_id": "vendor/small",
            "context_window": 1000,
            "supports_tools": True,
            "supports_vision": False,
            "efforts": ["none", "low", "high"],
            "effort_style": "suffix",
        },
        "big": {
            "upstream_id": "vendor/big",
            "context_window": 100000,
            "supports_tools": True,
            "supports_vision": True,
            "efforts": ["low", "medium", "high", "xhigh"],
            "effort_style": "suffix",
        },
        "paramy": {
            "upstream_id": "vendor/paramy",
            "context_window": 50000,
            "supports_tools": False,
            "supports_vision": False,
            "efforts": ["low", "high"],
            "effort_style": "param",
        },
    },
    "questions": {
        "task": {
            "type": "choice",
            "instructions": "What kind of work is this?",
            "criteria": {"code": "Code.", "quick_question": "A quick question."},
        },
        "difficulty": {
            "type": "score",
            "instructions": "How much work is this?",
            "criteria": ["Trivial.", "Small.", "Multi-step."],
        },
        "harm_if_wrong": {"type": "noul", "instructions": "Would a mistake cause harm?"},
    },
    "policy": {
        "low_confidence": {
            "min_confidence": 0.45,
            "questions": ["task"],
            "use": {"model": "big", "effort": "medium"},
        },
        "rules": [
            {
                "name": "harmful",
                "when": {"harm_if_wrong": {"gte": 0.6}},
                "use": {"model": "big", "effort": "medium"},
            },
            {
                "name": "hard",
                "when": {"difficulty": {"gte": 1.5}},
                "use": {"model": "big", "effort": "high"},
            },
            {
                "name": "confident_code",
                "when": {"task": {"in": ["code"], "conf_gte": 0.8}},
                "use": {"model": "big", "effort": "low"},
            },
            {
                "name": "tools",
                "when": {"has_tools": True},
                "use": {"model": "big", "effort": "medium"},
            },
            {
                "name": "easy",
                "when": {"difficulty": {"lte": 0.5}},
                "use": {"model": "small", "effort": "none"},
            },
        ],
        "default": {"model": "big", "effort": "medium"},
    },
    "aliases": {
        "auto": {
            "state_builder": "summary_v1",
            "questions": ["task", "difficulty", "harm_if_wrong"],
            "rules": "policy",
            "allowed_models": ["small", "big"],
        },
        "auto-fast": {
            "state_builder": "last_message_only",
            "questions": ["difficulty"],
            "rules": "policy",
            "allowed_models": ["small", "big"],
            "max_effort": "low",
        },
    },
    "clients": {
        "floored": {"min_effort": "high"},
        "bigonly": {"allowed_models": ["big"]},
    },
}


def make_config(**overrides: Any) -> RouterConfig:
    """Build a config from the sample, with a deep-merged override mapping."""
    raw = copy.deepcopy(BASE_CONFIG)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key] = _merge(raw[key], value)
        else:
            raw[key] = value
    cfg = RouterConfig.model_validate(raw)
    cfg._config_hash = "testhash"
    return cfg


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def write_config(tmp_path, raw: dict[str, Any] | None = None) -> str:
    path = tmp_path / "router.yaml"
    body = copy.deepcopy(raw if raw is not None else BASE_CONFIG)
    body["settings"]["sqlite_path"] = str(tmp_path / "router.db")
    path.write_text(yaml.safe_dump(body))
    return str(path)


@pytest.fixture
def config() -> RouterConfig:
    return make_config()


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "test.db")
    yield s
    s.close()

"""Circuit breakers, one per provider."""

from __future__ import annotations

import pytest

from conftest import make_config
from jev_router.providers import CLOSED, HALF_OPEN, OPEN, ProviderBreakers, merge_pressures


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


TWO_PROVIDERS = {
    "providers": {
        "cloud": {"description": "A"},
        "local": {"description": "B"},
    },
    "models": {
        "small": {"provider": "local"},
        "big": {"provider": "cloud"},
        "paramy": {"provider": "cloud"},
    },
}


@pytest.fixture
def two_providers():
    return make_config(**TWO_PROVIDERS)


@pytest.fixture
def breakers(two_providers):
    return ProviderBreakers(two_providers, clock=Clock())


def test_a_config_with_no_providers_has_one_implicit_provider(config):
    assert config.provider_names() == ["default"]
    assert config.provider_of("big") == "default"


def test_models_are_grouped_by_provider(two_providers):
    assert sorted(two_providers.provider_names()) == ["cloud", "local"]
    assert two_providers.provider_of("big") == "cloud"
    assert two_providers.provider_of("small") == "local"


def test_the_breaker_opens_only_after_enough_failures_in_a_row(breakers):
    for _ in range(2):
        breakers.record_failure("cloud", "HTTP 429")
    assert breakers.available("cloud") is True
    breakers.record_failure("cloud", "HTTP 429")
    assert breakers.available("cloud") is False
    assert breakers.report()["cloud"]["state"] == OPEN
    # One provider's trouble says nothing about another's.
    assert breakers.available("local") is True


def test_a_success_clears_the_run_of_failures(breakers):
    breakers.record_failure("cloud")
    breakers.record_failure("cloud")
    breakers.record_success("cloud")
    breakers.record_failure("cloud")
    breakers.record_failure("cloud")
    assert breakers.available("cloud") is True


def test_the_cooldown_ends_in_half_open_with_exactly_one_trial(two_providers):
    clock = Clock()
    breakers = ProviderBreakers(two_providers, clock=clock)
    for _ in range(3):
        breakers.record_failure("cloud")
    clock.tick(119)
    assert breakers.acquire("cloud") is False
    clock.tick(2)
    assert breakers.report()["cloud"]["state"] == HALF_OPEN
    assert breakers.acquire("cloud") is True     # the trial
    assert breakers.acquire("cloud") is False    # and only one
    breakers.record_success("cloud")
    assert breakers.report()["cloud"]["state"] == CLOSED
    assert breakers.acquire("cloud") is True


def test_a_failed_trial_doubles_the_cooldown_up_to_the_cap(two_providers):
    clock = Clock()
    breakers = ProviderBreakers(two_providers, clock=clock)
    for _ in range(3):
        breakers.record_failure("cloud")
    assert breakers.report()["cloud"]["cooldown_seconds"] == 120.0

    for expected in (240.0, 480.0, 960.0, 1920.0, 1920.0):
        clock.tick(breakers.states["cloud"].cooldown + 1)
        assert breakers.acquire("cloud") is True
        breakers.record_failure("cloud", "HTTP 503")
        assert breakers.report()["cloud"]["cooldown_seconds"] == expected


def test_pressure_is_full_while_open_and_decays_after_recovery(two_providers):
    clock = Clock()
    breakers = ProviderBreakers(two_providers, clock=clock)
    assert breakers.pressure("cloud") == 0.0
    for _ in range(3):
        breakers.record_failure("cloud")
    assert breakers.pressure("cloud") == 1.0

    clock.tick(121)
    assert breakers.acquire("cloud") is True
    breakers.record_success("cloud")
    assert breakers.pressure("cloud") == 1.0          # just recovered
    clock.tick(60)
    assert breakers.pressure("cloud") == pytest.approx(0.5, abs=0.02)
    clock.tick(61)
    assert breakers.pressure("cloud") == 0.0


def test_the_report_names_every_provider_and_its_models(breakers):
    report = breakers.report()
    assert set(report) == {"cloud", "local"}
    assert report["cloud"]["models"] == ["big", "paramy"]
    assert report["local"]["healthy"] is True
    assert report["cloud"]["failures_to_open"] == 3


def test_a_provider_with_no_quota_source_takes_its_pressure_from_health(two_providers):
    breakers = ProviderBreakers(two_providers, clock=Clock())
    for _ in range(3):
        breakers.record_failure("local")
    merged = merge_pressures(two_providers, {"cloud": 0.4}, breakers)
    assert merged == {"cloud": 0.4, "local": 1.0}


def test_a_provider_with_both_takes_the_higher_of_the_two(two_providers):
    breakers = ProviderBreakers(two_providers, clock=Clock())
    for _ in range(3):
        breakers.record_failure("cloud")
    merged = merge_pressures(two_providers, {"cloud": 0.3, "local": 0.1}, breakers)
    assert merged["cloud"] == 1.0
    assert merged["local"] == 0.1


def test_turning_quota_routing_off_leaves_no_pressure_at_all(two_providers):
    cfg = make_config(**TWO_PROVIDERS, quota_policy={"enabled": False})
    breakers = ProviderBreakers(cfg, clock=Clock())
    for _ in range(3):
        breakers.record_failure("cloud")
    assert merge_pressures(cfg, {"cloud": 0.9}, breakers) == {}

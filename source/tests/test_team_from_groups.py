# ABOUTME: Tests for the configurable attribution_map (dimension -> ordered sources)
# ABOUTME: Mirrors Go otel extract tests — legacy chains unchanged unless a deployment opts in

"""Attribution map tests (parity with Go otel extractor).

Legacy defaults are untouched (existing deployments keep working); a
deployment's attribution_map in config.json redefines what feeds team.id /
role / organization / department / cost_center. Source expressions:
claim:<name>, claims_sorted:<name> (alpha-sorted "|"-join), static:<key>
(OTEL_RESOURCE_ATTRIBUTES), literal:<value>.
"""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "otel_helper_main_groups",
    Path(__file__).resolve().parents[1] / "otel_helper" / "__main__.py",
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
extract_user_info = _module.extract_user_info


class TestLegacyDefaultChains:
    def test_group_membership_does_not_feed_role_or_change_team(self, monkeypatch):
        monkeypatch.setattr(_module, "_load_attribution_config", lambda: ({}, {}))
        result = extract_user_info({"email": "dev@corp.com", "group": "eng", "groups": ["engineering"]})
        assert result["team"] == "eng"  # legacy team -> team_id -> group chain
        assert result["role"] == "user"  # groups never feed role by default


class TestResolveAttributionSources:
    def _resolve(self, payload, sources):
        return _module._resolve_attribution_sources(payload, sources)

    def test_claim(self):
        assert self._resolve({"role": "developer"}, ["claim:role"]) == "developer"

    def test_claim_first_of_array(self):
        assert self._resolve({"groups": ["zeta", "alpha"]}, ["claim:groups"]) == "zeta"

    def test_claims_sorted_joins_alpha(self):
        assert self._resolve({"groups": ["zeta", "alpha", "mid"]}, ["claims_sorted:groups"]) == "alpha|mid|zeta"

    def test_static_env_fallback(self, monkeypatch):
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team.id=platform-eng")
        assert self._resolve({}, ["static:team.id"]) == "platform-eng"

    def test_static_config_wins_over_env(self, monkeypatch):
        """config.json statics resolve identically in every invocation context."""
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team.id=env-value")
        result = _module._resolve_attribution_sources({}, ["static:team.id"], {"team.id": "config-value"})
        assert result == "config-value"

    def test_literal_and_fallback_order(self, monkeypatch):
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        assert self._resolve({}, ["claim:absent", "static:missing", "literal:last"]) == "last"

    def test_malformed_source_skipped(self):
        assert self._resolve({"role": "dev"}, ["nonsense", "claim:role"]) == "dev"


class TestApplyAttributionMap:
    def test_overrides_and_empty_resolution_keeps_legacy(self, monkeypatch):
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team.id=platform-eng")
        monkeypatch.setattr(
            _module,
            "_load_attribution_config",
            lambda: (
                {
                    "team.id": ["static:team.id"],
                    "role": ["claims_sorted:groups"],
                    "cost_center": ["claim:absent_claim"],  # empty -> legacy kept
                },
                {},
            ),
        )
        result = extract_user_info({"email": "dev@corp.com", "team": "claim-team", "groups": ["zeta", "alpha"]})
        assert result["team"] == "platform-eng"  # deployment static per map
        assert result["role"] == "alpha|zeta"  # sorted joined groups
        assert result["cost_center"] == "general"  # empty resolution -> legacy
        assert result["department"] == "unspecified"  # unmapped dims untouched

    def test_missing_config_is_legacy(self, monkeypatch):
        monkeypatch.setattr(_module, "_load_attribution_config", lambda: ({}, {}))
        result = extract_user_info({"team": "platform"})
        assert result["team"] == "platform"
        assert result["role"] == "user"


class TestProfileRoundTrip:
    def test_attribution_map_round_trips_and_defaults_empty(self):
        from claude_code_with_bedrock.config import Profile

        p = Profile(
            name="t",
            provider_domain="d",
            client_id="c",
            credential_storage="session",
            aws_region="us-gov-west-1",
            identity_pool_name="p",
            attribution_map={"role": ["claims_sorted:groups"], "team.id": ["static:team.id"]},
        )
        loaded = Profile.from_dict(p.to_dict())
        assert loaded.attribution_map == {"role": ["claims_sorted:groups"], "team.id": ["static:team.id"]}

        data = p.to_dict()
        data.pop("attribution_map")
        assert Profile.from_dict(data).attribution_map == {}  # old profiles load

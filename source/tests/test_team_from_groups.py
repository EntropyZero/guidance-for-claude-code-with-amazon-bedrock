# ABOUTME: Tests for team/role attribution precedence (claims > groups array > static env)
# ABOUTME: Mirrors Go otel extract tests — group-based cost dashboards aggregate by role

"""Team/role precedence (parity with Go otel extractor).

Role = role claim > singular group claim > groups[0] > static env role > "user"
Team = team claim > department claim > static env team > env team.id > "default-team"

Group membership feeds ROLE, never team: team is claim-then-deployment-owned
(static OTEL_RESOURCE_ATTRIBUTES), and the helper resolves the precedence at
header-generation time since it runs inside Claude Code's environment.
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


class TestRolePrecedence:
    def test_role_claim_wins(self, monkeypatch):
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "role=static-role")
        result = extract_user_info({"role": "developer", "group": "eng", "groups": ["x"]})
        assert result["role"] == "developer"

    def test_singular_group_beats_groups_array(self, monkeypatch):
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        result = extract_user_info({"group": "eng", "groups": ["other"]})
        assert result["role"] == "eng"

    def test_first_groups_entry_used(self, monkeypatch):
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        result = extract_user_info({"email": "dev@corp.com", "groups": ["engineering", "ai-team"]})
        assert result["role"] == "engineering"

    def test_env_role_when_no_claims(self, monkeypatch):
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "role=static-role")
        assert extract_user_info({})["role"] == "static-role"

    def test_default_when_nothing(self, monkeypatch):
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        assert extract_user_info({"groups": []})["role"] == "user"

    def test_non_string_groups_entries_skipped(self, monkeypatch):
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        assert extract_user_info({"groups": [42, False, "ai-team"]})["role"] == "ai-team"


class TestTeamPrecedence:
    def test_team_claim_wins(self, monkeypatch):
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team=static-team")
        result = extract_user_info({"team": "platform", "department": "cloud"})
        assert result["team"] == "platform"

    def test_department_claim_beats_env(self, monkeypatch):
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team=static-team")
        assert extract_user_info({"department": "cloud"})["team"] == "cloud"

    def test_env_team_when_no_claims(self, monkeypatch):
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team=static-team,team.id=legacy")
        assert extract_user_info({})["team"] == "static-team"

    def test_env_team_id_fallback(self, monkeypatch):
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team.id=legacy-static")
        assert extract_user_info({})["team"] == "legacy-static"

    def test_groups_never_feed_team(self, monkeypatch):
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team=static-team")
        result = extract_user_info({"group": "eng", "groups": ["engineering"]})
        assert result["team"] == "static-team"

    def test_default_when_nothing(self, monkeypatch):
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        assert extract_user_info({})["team"] == "default-team"

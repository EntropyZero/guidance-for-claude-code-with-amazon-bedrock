# ABOUTME: Tests for role attribution falling back to the groups array claim
# ABOUTME: Mirrors Go otel extract tests — group-based cost dashboards aggregate by role

"""Role must fall back to the first "groups" array entry (parity with Go).

IdPs like Okta send group membership as an array. The group lands in the ROLE
attribute (not team): team.id is commonly customized per client deployment via
static OTEL_RESOURCE_ATTRIBUTES, and overwriting it from claims would clobber
that deployment-owned value.
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


class TestRoleFromGroupsArray:
    def test_first_groups_entry_used_for_role(self):
        result = extract_user_info({"email": "dev@corp.com", "groups": ["engineering", "ai-team"]})
        assert result["role"] == "engineering"

    def test_groups_do_not_feed_team(self):
        """team.id stays deployment-owned — groups must not clobber it."""
        result = extract_user_info({"email": "dev@corp.com", "groups": ["engineering"]})
        assert result["team"] == "default-team"

    def test_singular_role_claim_wins(self):
        result = extract_user_info({"role": "developer", "groups": ["engineering"]})
        assert result["role"] == "developer"

    def test_title_claim_wins_over_groups(self):
        result = extract_user_info({"title": "engineer-ii", "groups": ["engineering"]})
        assert result["role"] == "engineer-ii"

    def test_empty_groups_falls_back_to_default(self):
        assert extract_user_info({"groups": []})["role"] == "user"

    def test_non_string_groups_entries_skipped(self):
        assert extract_user_info({"groups": [42, False, "ai-team"]})["role"] == "ai-team"

    def test_string_groups_claim_used_verbatim(self):
        assert extract_user_info({"groups": "engineering"})["role"] == "engineering"

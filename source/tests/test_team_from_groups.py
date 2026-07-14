# ABOUTME: Tests for team attribution falling back to the groups array claim
# ABOUTME: Mirrors Go otel extract tests — group-based cost dashboards need team.id populated

"""Team must fall back to the first "groups" array entry (parity with Go).

IdPs like Okta send group membership as an array; without this fallback every
such user collapsed to "default-team" and per-group cost aggregation on
dashboards showed a single meaningless bucket.
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


class TestTeamFromGroupsArray:
    def test_first_groups_entry_used(self):
        result = extract_user_info({"email": "dev@corp.com", "groups": ["engineering", "ai-team"]})
        assert result["team"] == "engineering"

    def test_singular_team_claim_wins(self):
        result = extract_user_info({"team": "platform", "groups": ["engineering"]})
        assert result["team"] == "platform"

    def test_empty_groups_falls_back_to_default(self):
        assert extract_user_info({"groups": []})["team"] == "default-team"

    def test_non_string_groups_entries_skipped(self):
        assert extract_user_info({"groups": [42, False, "ai-team"]})["team"] == "ai-team"

    def test_string_groups_claim_used_verbatim(self):
        assert extract_user_info({"groups": "engineering"})["team"] == "engineering"

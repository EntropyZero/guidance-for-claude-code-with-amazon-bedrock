# ABOUTME: Tests for 'ccwb quota set' unified command that routes to set-user/set-group/set-default
# ABOUTME: Verifies argument routing, validation, and error messages

"""Tests for the unified 'quota set' command alias.

Addresses #634 comment: users expect 'ccwb quota set user@co.com --budget 50'
syntax as documented in PR #633, but only set-user/set-group/set-default existed.
"""

from unittest.mock import MagicMock, patch

import pytest
from cleo.testers.application_tester import ApplicationTester

from claude_code_with_bedrock.cli import create_application


class TestQuotaSetRouting:
    """Verify quota set routes to the correct subcommand."""

    @pytest.fixture
    def app_tester(self):
        app = create_application()
        return ApplicationTester(app)

    def test_no_args_shows_helpful_error(self, app_tester):
        """quota set with no args tells user what to provide."""
        app_tester.execute("quota set")
        output = app_tester.io.fetch_output()
        assert app_tester.status_code == 1
        assert "Email is required" in output

    def test_invalid_email_suggests_group(self, app_tester):
        """quota set with non-email suggests --group."""
        app_tester.execute("quota set engineering")
        output = app_tester.io.fetch_output()
        assert app_tester.status_code == 1
        assert "--group" in output

    def test_both_group_and_default_rejects(self, app_tester):
        """quota set --group --default is an error."""
        app_tester.execute("quota set --group --default blah")
        output = app_tester.io.fetch_output()
        assert app_tester.status_code == 1
        assert "Cannot use both" in output

    def test_group_without_identifier_shows_error(self, app_tester):
        """quota set --group without a name shows error."""
        app_tester.execute("quota set --group")
        output = app_tester.io.fetch_output()
        assert app_tester.status_code == 1
        assert "Group name is required" in output

    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_routes_to_set_user(self, mock_config_cls, mock_get_manager):
        """quota set user@co.com --budget 50 --monthly-limit 1B routes to set-user."""
        mock_config = MagicMock()
        mock_profile = MagicMock()
        mock_profile.aws_region = "us-west-2"
        mock_config.active_profile = "default"
        mock_config.get_profile.return_value = mock_profile
        mock_config_cls.load.return_value = mock_config

        mock_manager = MagicMock()
        mock_policy = MagicMock()
        mock_policy.monthly_token_limit = 1_000_000_000
        mock_policy.daily_token_limit = None
        mock_policy.enforcement_mode = MagicMock(value="alert")
        mock_policy.daily_enforcement_mode = MagicMock(value="alert")
        mock_manager.create_policy.return_value = mock_policy
        mock_get_manager.return_value = mock_manager

        app = create_application()
        tester = ApplicationTester(app)
        tester.execute("quota set alice@company.com --budget 50 --monthly-limit 1B")

        assert tester.status_code == 0
        mock_manager.create_policy.assert_called_once()

    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_routes_to_set_group(self, mock_config_cls, mock_get_manager):
        """quota set --group engineering --budget 200 --monthly-limit 1B routes to set-group."""
        mock_config = MagicMock()
        mock_profile = MagicMock()
        mock_profile.aws_region = "us-west-2"
        mock_config.active_profile = "default"
        mock_config.get_profile.return_value = mock_profile
        mock_config_cls.load.return_value = mock_config

        mock_manager = MagicMock()
        mock_policy = MagicMock()
        mock_policy.monthly_token_limit = 1_000_000_000
        mock_policy.daily_token_limit = None
        mock_policy.enforcement_mode = MagicMock(value="alert")
        mock_policy.daily_enforcement_mode = MagicMock(value="alert")
        mock_manager.create_policy.return_value = mock_policy
        mock_get_manager.return_value = mock_manager

        app = create_application()
        tester = ApplicationTester(app)
        tester.execute("quota set --group engineering --budget 200 --monthly-limit 1B")

        assert tester.status_code == 0
        mock_manager.create_policy.assert_called_once()

    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_routes_to_set_default(self, mock_config_cls, mock_get_manager):
        """quota set --default --budget 30 --monthly-limit 1B routes to set-default."""
        mock_config = MagicMock()
        mock_profile = MagicMock()
        mock_profile.aws_region = "us-west-2"
        mock_config.active_profile = "default"
        mock_config.get_profile.return_value = mock_profile
        mock_config_cls.load.return_value = mock_config

        mock_manager = MagicMock()
        mock_policy = MagicMock()
        mock_policy.monthly_token_limit = 1_000_000_000
        mock_policy.daily_token_limit = None
        mock_policy.enforcement_mode = MagicMock(value="alert")
        mock_policy.daily_enforcement_mode = MagicMock(value="alert")
        mock_manager.create_policy.return_value = mock_policy
        mock_get_manager.return_value = mock_manager

        app = create_application()
        tester = ApplicationTester(app)
        tester.execute("quota set --default --budget 30 --monthly-limit 1B")

        assert tester.status_code == 0
        mock_manager.create_policy.assert_called_once()

    def test_quota_help_lists_set_command(self, app_tester):
        """ccwb quota shows 'quota set' in subcommand list."""
        app_tester.execute("quota")
        output = app_tester.io.fetch_output()
        assert "quota set" in output
        assert "Set quota (user, group, or default)" in output

    def test_help_shows_unified_options(self, app_tester):
        """quota set --help shows --group, --default, --budget."""
        app_tester.execute("quota set --help")
        output = app_tester.io.fetch_output()
        assert "--group" in output
        assert "--default" in output
        assert "--budget" in output


class TestCostOnlyPolicies:
    """Budget-only invocations must work without a token limit.

    Regression: set-user/set-group/set-default hard-failed with
    "--monthly-limit is required" when only --budget was given, making
    cost-only policies impossible to create from the CLI — the QuotaPolicies
    items never got their cost attributes. A cost-only policy now stores
    monthly_token_limit=0 (token limits disabled; the budget governs).
    """

    def _mock_manager(self, mock_config_cls, mock_get_manager):
        mock_config = MagicMock()
        mock_profile = MagicMock()
        mock_profile.aws_region = "us-gov-west-1"
        mock_config.active_profile = "default"
        mock_config.get_profile.return_value = mock_profile
        mock_config_cls.load.return_value = mock_config

        mock_manager = MagicMock()
        mock_policy = MagicMock()
        mock_policy.monthly_token_limit = 0
        mock_policy.daily_token_limit = None
        mock_policy.enforcement_mode = MagicMock(value="block")
        mock_policy.daily_enforcement_mode = MagicMock(value="alert")
        mock_manager.create_policy.return_value = mock_policy
        mock_get_manager.return_value = mock_manager
        return mock_manager

    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_set_user_budget_only(self, mock_config_cls, mock_get_manager):
        mock_manager = self._mock_manager(mock_config_cls, mock_get_manager)

        app = create_application()
        tester = ApplicationTester(app)
        tester.execute("quota set-user alice@company.com --budget 50")

        assert tester.status_code == 0
        kwargs = mock_manager.create_policy.call_args.kwargs
        assert kwargs["monthly_token_limit"] == 0
        # Cost limits are first-class create_policy arguments
        assert kwargs["monthly_cost_limit"] == 50.0

    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_set_group_budget_only(self, mock_config_cls, mock_get_manager):
        mock_manager = self._mock_manager(mock_config_cls, mock_get_manager)

        app = create_application()
        tester = ApplicationTester(app)
        tester.execute("quota set-group engineering --budget 200 --daily-budget 20")

        assert tester.status_code == 0
        kwargs = mock_manager.create_policy.call_args.kwargs
        assert kwargs["monthly_token_limit"] == 0
        assert kwargs["monthly_cost_limit"] == 200.0
        assert kwargs["daily_cost_limit"] == 20.0

    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_set_default_budget_only(self, mock_config_cls, mock_get_manager):
        mock_manager = self._mock_manager(mock_config_cls, mock_get_manager)

        app = create_application()
        tester = ApplicationTester(app)
        tester.execute("quota set-default --budget 30")

        assert tester.status_code == 0
        kwargs = mock_manager.create_policy.call_args.kwargs
        assert kwargs["monthly_token_limit"] == 0
        assert kwargs["monthly_cost_limit"] == 30.0

    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_no_limits_at_all_still_errors(self, mock_config_cls, mock_get_manager, capsys):
        """set-default with neither token nor cost limit keeps the clear error."""
        mock_manager = self._mock_manager(mock_config_cls, mock_get_manager)

        app = create_application()
        tester = ApplicationTester(app)
        tester.execute("quota set-default")

        assert tester.status_code == 1
        # Error prints via rich Console (real stdout), not cleo's captured io
        assert "--monthly-limit or --budget is required" in capsys.readouterr().out
        mock_manager.create_policy.assert_not_called()

"""
Regression test for issue #655: Go credential-process quota warnings must fire
on all 4 auth paths, same as Python.

Tests that _handle_quota_warning is called on every quota-check path in the
Python implementation (baseline parity for Go fix).
"""

import os
import sys
from unittest.mock import MagicMock

import pytest


class TestQuotaWarningParity:
    """Verify Python _handle_quota_warning fires at 80%+ on all paths."""

    @pytest.fixture
    def mock_provider(self):
        """Create a minimal mock of the credential provider."""
        # Import the module
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from credential_provider.__main__ import MultiProviderAuth

        provider = MagicMock(spec=MultiProviderAuth)
        provider._handle_quota_warning = MultiProviderAuth._handle_quota_warning.__get__(provider, MultiProviderAuth)
        provider._print_quota_usage_lines = MultiProviderAuth._print_quota_usage_lines.__get__(
            provider, MultiProviderAuth
        )
        provider._show_quota_browser_notification = MagicMock()
        return provider

    def test_warning_fires_at_80_percent_monthly(self, mock_provider, capsys):
        """Warning should fire when monthly usage is at 80%."""
        quota_result = {
            "allowed": True,
            "usage": {
                "monthly_percent": 80.0,
                "daily_percent": 50.0,
                "monthly_tokens": 32000000,
                "monthly_limit": 40000000,
                "daily_tokens": 1000000,
                "daily_limit": 2000000,
            },
        }
        mock_provider._handle_quota_warning(quota_result)
        captured = capsys.readouterr()
        assert "QUOTA WARNING" in captured.err

    def test_warning_fires_at_80_percent_daily(self, mock_provider, capsys):
        """Warning should fire when daily usage is at 80%."""
        quota_result = {
            "allowed": True,
            "usage": {
                "monthly_percent": 50.0,
                "daily_percent": 80.0,
                "monthly_tokens": 20000000,
                "monthly_limit": 40000000,
                "daily_tokens": 1600000,
                "daily_limit": 2000000,
            },
        }
        mock_provider._handle_quota_warning(quota_result)
        captured = capsys.readouterr()
        assert "QUOTA WARNING" in captured.err

    def test_warning_fires_over_100_percent(self, mock_provider, capsys):
        """Warning should fire when daily usage exceeds 100% (451.9% case from #655)."""
        quota_result = {
            "allowed": True,
            "usage": {
                "monthly_percent": 22.7,
                "daily_percent": 451.9,
                "monthly_tokens": 9100000,
                "monthly_limit": 40000000,
                "daily_tokens": 9000000,
                "daily_limit": 2000000,
            },
        }
        mock_provider._handle_quota_warning(quota_result)
        captured = capsys.readouterr()
        assert "QUOTA WARNING" in captured.err
        assert "451.9%" in captured.err

    def test_no_warning_below_threshold(self, mock_provider, capsys):
        """No warning below 80% on both metrics."""
        quota_result = {
            "allowed": True,
            "usage": {
                "monthly_percent": 50.0,
                "daily_percent": 50.0,
                "monthly_tokens": 20000000,
                "monthly_limit": 40000000,
                "daily_tokens": 1000000,
                "daily_limit": 2000000,
            },
        }
        mock_provider._handle_quota_warning(quota_result)
        captured = capsys.readouterr()
        assert "QUOTA WARNING" not in captured.err

    def test_no_warning_on_empty_usage(self, mock_provider, capsys):
        """No warning when usage is empty."""
        quota_result = {"allowed": True, "usage": {}}
        mock_provider._handle_quota_warning(quota_result)
        captured = capsys.readouterr()
        assert "QUOTA WARNING" not in captured.err

    def test_no_warning_on_none_usage(self, mock_provider, capsys):
        """No warning when usage is None."""
        quota_result = {"allowed": True, "usage": None}
        mock_provider._handle_quota_warning(quota_result)
        captured = capsys.readouterr()
        assert "QUOTA WARNING" not in captured.err


class TestCostModeDisplayParity:
    """Cost-mode usage lines must render spend, not "0 / 0 tokens".

    Mirrors the Go helper's TestPrintQuotaUsageLines_CostMode — the two
    helpers must show the same information (credential-helper-parity.md).
    """

    @pytest.fixture
    def mock_provider(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from credential_provider.__main__ import MultiProviderAuth

        provider = MagicMock(spec=MultiProviderAuth)
        provider._handle_quota_warning = MultiProviderAuth._handle_quota_warning.__get__(provider, MultiProviderAuth)
        provider._print_quota_usage_lines = MultiProviderAuth._print_quota_usage_lines.__get__(
            provider, MultiProviderAuth
        )
        provider._show_quota_browser_notification = MagicMock()
        return provider

    def test_cost_mode_warning_shows_spend_not_tokens(self, mock_provider, capsys):
        quota_result = {
            "allowed": True,
            "usage": {
                "monthly_percent": 85.0,
                "monthly_tokens": 12000000,
                "monthly_limit": 0,  # cost mode: token limits disabled
                "daily_tokens": 400000,
                "monthly_cost": 42.5,
                "monthly_cost_limit": 50.0,
                "monthly_cost_percent": 85.0,
                "daily_cost": 3.1,
                "daily_cost_limit": 5.0,
                "daily_cost_percent": 62.0,
            },
        }
        mock_provider._handle_quota_warning(quota_result)
        captured = capsys.readouterr()
        assert "QUOTA WARNING" in captured.err
        assert "Monthly spend: $42.50 / $50.00 (85.0%)" in captured.err
        assert "Daily spend: $3.10 / $5.00 (62.0%)" in captured.err
        assert "tokens" not in captured.err

    def test_token_mode_unchanged(self, mock_provider, capsys):
        quota_result = {
            "allowed": True,
            "usage": {
                "monthly_percent": 85.0,
                "monthly_tokens": 34000000,
                "monthly_limit": 40000000,
                "daily_tokens": 900000,
                "daily_percent": 45.0,
                "daily_limit": 2000000,
                "monthly_cost": 12.34,  # spend reported but no cost limit
            },
        }
        mock_provider._handle_quota_warning(quota_result)
        captured = capsys.readouterr()
        assert "Monthly: 34,000,000 / 40,000,000 tokens (85.0%)" in captured.err
        assert "spend" not in captured.err

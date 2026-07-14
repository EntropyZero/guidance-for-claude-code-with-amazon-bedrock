# ABOUTME: Regression tests for cost-based quota wiring: wizard answers must reach
# ABOUTME: the profile, the stack, and both Lambdas (previously dropped end-to-end).

"""Cost-based quota was half-wired: the wizard asked for $ budgets but the
answers were dropped at every hop.

- No Profile fields existed for limit type / cost limits → not in the profile
  JSON, lost on re-init.
- deploy never passed MonthlyCostLimitUsd / DailyCostLimitUsd; the template
  declared them but wired them to nothing.
- quota_check's environment-default policy activated only when
  MONTHLY_TOKEN_LIMIT > 0 — cost mode zeroes token limits, so cost-mode
  deployments resolved NO policy and every user was unlimited.
- quota_monitor alerted `total_tokens > monthly_limit` with no zero guard, so
  a 0 token limit (cost mode) alert-stormed every active user each scan.
- The wizard also asked for token counts in cost mode (the mode gate only
  wrapped a header print) and silently dropped the monthly enforcement answer.

These tests pin every hop of the repaired chain.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

import yaml

# ruff: noqa: E402
sys.path.insert(0, str(Path(__file__).parent.parent))

from claude_code_with_bedrock.cli.commands.init import InitCommand
from claude_code_with_bedrock.config import Config, Profile

LAMBDA_DIR = Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "lambda-functions"
TEMPLATE = Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "quota-monitoring.yaml"


def _load_lambda(name: str, env: dict):
    """Load a Lambda module fresh with the given environment (loader pattern
    from test_quota_monitor_lambda.py). Restores os.environ afterwards so
    later tests loading the same Lambdas don't inherit these limits."""
    base = {
        "AWS_DEFAULT_REGION": "us-gov-west-1",
        "QUOTA_TABLE": "TestQuotaTable",
        "POLICIES_TABLE": "TestPoliciesTable",
        "SNS_TOPIC_ARN": "arn:aws-us-gov:sns:us-gov-west-1:123456789012:test-alerts",
    }
    base.update(env)
    prior = {key: os.environ.get(key) for key in base}
    for key, value in base.items():
        os.environ[key] = str(value)
    try:
        module_name = f"{name}_cost_{abs(hash(frozenset((k, str(v)) for k, v in base.items())))}"
        spec = importlib.util.spec_from_file_location(module_name, LAMBDA_DIR / name / "index.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestProfileFields:
    def test_cost_fields_round_trip(self):
        p = Profile(
            name="t",
            provider_domain="d",
            client_id="c",
            credential_storage="session",
            aws_region="us-gov-west-1",
            identity_pool_name="p",
            quota_limit_type="cost",
            monthly_cost_limit_usd=50.0,
            daily_cost_limit_usd=5.0,
        )
        loaded = Profile.from_dict(p.to_dict())
        assert loaded.quota_limit_type == "cost"
        assert loaded.monthly_cost_limit_usd == 50.0
        assert loaded.daily_cost_limit_usd == 5.0

    def test_old_profiles_default_to_token_mode(self):
        data = Profile(
            name="t",
            provider_domain="d",
            client_id="c",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="p",
        ).to_dict()
        for key in ("quota_limit_type", "monthly_cost_limit_usd", "daily_cost_limit_usd"):
            data.pop(key)
        loaded = Profile.from_dict(data)
        assert loaded.quota_limit_type == "token"
        assert loaded.monthly_cost_limit_usd == 0
        assert loaded.daily_cost_limit_usd == 0


class TestInitRoundTrip:
    def _rebuild(self, profile: Profile) -> dict:
        command = InitCommand()
        fake_config = Config()
        with (
            patch.object(Config, "load", return_value=fake_config),
            patch.object(fake_config, "get_profile", return_value=profile),
            patch.object(InitCommand, "_stack_exists", side_effect=Exception("no creds")),
        ):
            return command._check_existing_deployment("test")

    def test_rerun_preserves_cost_quota_fields(self):
        profile = Profile(
            name="test",
            provider_domain="example.okta.com",
            client_id="0oa1234567890",
            identity_pool_name="claude-code-auth",
            credential_storage="keyring",
            aws_region="us-gov-west-1",
            quota_monitoring_enabled=True,
            quota_limit_type="cost",
            monthly_cost_limit_usd=75.0,
            daily_cost_limit_usd=10.0,
            monthly_token_limit=0,
        )
        quota = self._rebuild(profile)["quota"]
        assert quota["limit_type"] == "cost"
        assert quota["monthly_cost_limit"] == 75.0
        assert quota["daily_cost_limit"] == 10.0

    def test_rerun_preserves_enable_finegrained_quotas(self):
        """enable_finegrained_quotas must survive a wizard re-run.

        Regression: the field was never saved by the wizard nor restored by
        _check_existing_deployment, so it could only be enabled by hand-editing
        the saved config — and a re-init would then silently reset it to False.
        """
        profile = Profile(
            name="test",
            provider_domain="example.okta.com",
            client_id="0oa1234567890",
            identity_pool_name="claude-code-auth",
            credential_storage="keyring",
            aws_region="us-gov-west-1",
            quota_monitoring_enabled=True,
            enable_finegrained_quotas=True,
        )
        quota = self._rebuild(profile)["quota"]
        assert quota["enable_finegrained"] is True

    def test_rerun_preserves_quota_fail_mode(self):
        """quota_fail_mode must survive a wizard re-run (now wizard-managed)."""
        profile = Profile(
            name="test",
            provider_domain="example.okta.com",
            client_id="0oa1234567890",
            identity_pool_name="claude-code-auth",
            credential_storage="keyring",
            aws_region="us-gov-west-1",
            quota_monitoring_enabled=True,
            quota_fail_mode="closed",
        )
        quota = self._rebuild(profile)["quota"]
        assert quota["fail_mode"] == "closed"


def _load_template() -> dict:
    class CFNLoader(yaml.SafeLoader):
        pass

    def _cfn(loader, suffix, node):  # noqa: ARG001 - yaml constructor signature
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    CFNLoader.add_multi_constructor("!", _cfn)
    return yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=CFNLoader)


class TestTemplateWiring:
    def test_cost_params_wired_to_both_lambdas(self):
        resources = _load_template()["Resources"]
        for fn in ("QuotaCheckFunction", "QuotaMonitorFunction"):
            env = resources[fn]["Properties"]["Environment"]["Variables"]
            assert env.get("MONTHLY_COST_LIMIT_USD") == "MonthlyCostLimitUsd", fn
            assert env.get("DAILY_COST_LIMIT_USD") == "DailyCostLimitUsd", fn

    def test_monthly_token_limit_accepts_zero(self):
        """Cost mode passes MonthlyTokenLimit=0; MinValue must allow it."""
        params = _load_template()["Parameters"]
        assert params["MonthlyTokenLimit"]["MinValue"] == 0


class TestQuotaCheckCostDefaults:
    def test_cost_only_env_resolves_default_policy(self):
        """The regression: cost mode (token limit 0) resolved NO policy →
        every user unlimited. A cost limit alone must activate the default."""
        mod = _load_lambda(
            "quota_check",
            {"MONTHLY_TOKEN_LIMIT": "0", "MONTHLY_COST_LIMIT_USD": "50", "DAILY_COST_LIMIT_USD": "5"},
        )
        policy = mod.resolve_quota_for_user("user@example.gov", [])
        assert policy is not None, "cost-only config must resolve the default policy"
        assert policy["monthly_cost_limit"] == 50.0
        assert policy["daily_cost_limit"] == 5.0
        assert policy["monthly_token_limit"] == 0

    def test_no_limits_resolves_no_policy(self):
        mod = _load_lambda("quota_check", {"MONTHLY_TOKEN_LIMIT": "0", "MONTHLY_COST_LIMIT_USD": "0"})
        assert mod.resolve_quota_for_user("user@example.gov", []) is None


class TestQuotaMonitorCostAlerts:
    ENV = {
        "MONTHLY_TOKEN_LIMIT": "0",
        "MONTHLY_COST_LIMIT_USD": "100",
        "DAILY_COST_LIMIT_USD": "10",
        "ENABLE_FINEGRAINED_QUOTAS": "false",
    }

    def _alerts(self, mod, *, total_tokens=0, daily_tokens=0, monthly_cost=0.0, daily_cost=0.0):
        policy = mod.resolve_user_quota("u@example.gov", [], {})
        return mod.check_limits_and_generate_alerts(
            email="u@example.gov",
            total_tokens=total_tokens,
            daily_tokens=daily_tokens,
            policy=policy,
            month_name="July 2026",
            current_date="2026-07-13",
            days_remaining=18,
            days_in_month=31,
            sent_alerts=set(),
            monthly_cost=monthly_cost,
            daily_cost=daily_cost,
        )

    def test_zero_token_limit_generates_no_token_alerts(self):
        """The alert-storm regression: with monthly_limit=0, any usage
        previously produced a 'monthly exceeded' alert every scan."""
        mod = _load_lambda("quota_monitor", self.ENV)
        alerts = self._alerts(mod, total_tokens=5_000_000, monthly_cost=1.0)
        assert not [a for a in alerts if a["alert_type"] == "monthly"], alerts

    def test_cost_warning_and_exceeded_levels(self):
        mod = _load_lambda("quota_monitor", self.ENV)
        warn = self._alerts(mod, monthly_cost=85.0)
        assert [a for a in warn if a["alert_type"] == "monthly_cost" and a["alert_level"] == "warning"]
        over = self._alerts(mod, monthly_cost=101.0)
        assert [a for a in over if a["alert_type"] == "monthly_cost" and a["alert_level"] == "exceeded"]

    def test_daily_cost_alert_carries_date(self):
        mod = _load_lambda("quota_monitor", self.ENV)
        alerts = self._alerts(mod, daily_cost=11.0)
        daily = [a for a in alerts if a["alert_type"] == "daily_cost"]
        assert daily and daily[0]["date"] == "2026-07-13"

    def test_usage_entry_includes_cost_with_stale_day_guard(self):
        mod = _load_lambda("quota_monitor", self.ENV)
        item = {
            "total_tokens": 1000,
            "daily_tokens": 500,
            "estimated_cost": 42.5,
            "daily_cost_usd": 7.5,
            "daily_date": "2026-07-12",
        }
        entry = mod._build_usage_entry(item, "2026-07-13")
        assert entry["monthly_cost"] == 42.5
        assert entry["daily_cost"] == 0, "stale-day guard must reset the daily cost counter"
        entry_today = mod._build_usage_entry({**item, "daily_date": "2026-07-13"}, "2026-07-13")
        assert entry_today["daily_cost"] == 7.5


class TestCostUsageSummary:
    """quota_check must report cost data in the usage summary.

    Regression: build_usage_summary was token-only. In cost mode (token
    limits 0) monthly_percent was always 0, so the credential helpers never
    showed the 80/90% warning and a blocked user saw "0 / 0 tokens".
    """

    ENV = {"MONTHLY_TOKEN_LIMIT": "0", "MONTHLY_COST_LIMIT_USD": "50", "DAILY_COST_LIMIT_USD": "5"}

    def _summary(self, usage_overrides=None):
        mod = _load_lambda("quota_check", self.ENV)
        policy = mod.resolve_quota_for_user("user@example.gov", [])
        usage = {
            "total_tokens": 12_000_000,
            "daily_tokens": 400_000,
            "estimated_cost": 42.5,
            "daily_cost_usd": 3.1,
        }
        usage.update(usage_overrides or {})
        return mod.build_usage_summary(usage, policy)

    def test_cost_fields_present(self):
        summary = self._summary()
        assert summary["monthly_cost"] == 42.5
        assert summary["monthly_cost_limit"] == 50.0
        assert summary["monthly_cost_percent"] == 85.0
        assert summary["daily_cost"] == 3.1
        assert summary["daily_cost_limit"] == 5.0
        assert summary["daily_cost_percent"] == 62.0

    def test_generic_percent_aliased_to_cost_in_cost_mode(self):
        """The helpers' 80/90% warning displays read monthly_percent/daily_percent."""
        summary = self._summary()
        assert summary["monthly_percent"] == 85.0
        assert summary["daily_percent"] == 62.0

    def test_token_mode_percent_not_aliased(self):
        mod = _load_lambda("quota_check", {"MONTHLY_TOKEN_LIMIT": "40000000", "MONTHLY_COST_LIMIT_USD": "0"})
        policy = mod.resolve_quota_for_user("user@example.gov", [])
        summary = mod.build_usage_summary({"total_tokens": 10_000_000, "daily_tokens": 0}, policy)
        assert summary["monthly_percent"] == 25.0
        assert "monthly_cost_limit" not in summary
        # Spend is still reported (0 with no usage data) for visibility
        assert summary["monthly_cost"] == 0


class TestMonitorStatsCostMode:
    """quota_monitor summary stats must use the governing (cost) limit."""

    def test_monthly_percent_uses_cost_when_token_limit_zero(self):
        mod = _load_lambda("quota_monitor", {"MONTHLY_TOKEN_LIMIT": "0", "MONTHLY_COST_LIMIT_USD": "50"})
        policy = {"monthly_token_limit": 0, "monthly_cost_limit": 50.0}
        usage = {"total_tokens": 12_000_000, "monthly_cost": 46.0}
        assert mod._monthly_usage_percent(usage, policy) == 92.0

    def test_monthly_percent_prefers_token_limit_when_set(self):
        mod = _load_lambda("quota_monitor", {"MONTHLY_TOKEN_LIMIT": "40000000"})
        policy = {"monthly_token_limit": 40_000_000, "monthly_cost_limit": 50.0}
        usage = {"total_tokens": 10_000_000, "monthly_cost": 46.0}
        assert mod._monthly_usage_percent(usage, policy) == 25.0

    def test_monthly_percent_zero_when_no_limits(self):
        mod = _load_lambda("quota_monitor", {"MONTHLY_TOKEN_LIMIT": "0"})
        assert mod._monthly_usage_percent({"total_tokens": 5}, {"monthly_token_limit": 0}) == 0


class TestDefaultPolicySeeding:
    """Deploy-time default policy seeding must carry cost limits.

    Regression: _create_default_quota_policy only wrote token fields. In cost
    mode the profile's token limits are 0, so the seeded default:default item
    had monthly_token_limit=0 and NO cost attributes — no limits at all — and
    create-only seeding meant redeploys never repaired it.
    """

    def _seed(self, profile):
        from unittest.mock import MagicMock

        from claude_code_with_bedrock.cli.commands.deploy import DeployCommand

        command = DeployCommand()
        mock_manager = MagicMock()
        with (
            patch(
                "claude_code_with_bedrock.cli.commands.deploy.get_stack_outputs",
                return_value={"PoliciesTableName": "QuotaPolicies"},
            ),
            patch("claude_code_with_bedrock.quota_policies.QuotaPolicyManager", return_value=mock_manager),
        ):
            command._create_default_quota_policy(profile, "quota-stack", MagicMock())
        return mock_manager

    def _cost_profile(self):
        return Profile(
            name="test",
            provider_domain="example.okta.com",
            client_id="0oa1234567890",
            identity_pool_name="claude-code-auth",
            credential_storage="keyring",
            aws_region="us-gov-west-1",
            quota_monitoring_enabled=True,
            quota_limit_type="cost",
            monthly_token_limit=0,
            monthly_cost_limit_usd=50.0,
            daily_cost_limit_usd=5.0,
            monthly_enforcement_mode="block",
        )

    def test_cost_mode_seeds_cost_limits(self):
        manager = self._seed(self._cost_profile())

        create_kwargs = manager.create_policy.call_args.kwargs
        assert create_kwargs["monthly_token_limit"] == 0
        assert create_kwargs["monthly_cost_limit"] == 50.0
        assert create_kwargs["daily_cost_limit"] == 5.0

    def test_token_mode_does_not_write_cost_attributes(self):
        profile = self._cost_profile()
        profile.quota_limit_type = "token"
        profile.monthly_token_limit = 225_000_000
        profile.monthly_cost_limit_usd = 0.0
        profile.daily_cost_limit_usd = 0.0

        manager = self._seed(profile)

        create_kwargs = manager.create_policy.call_args.kwargs
        assert create_kwargs["monthly_token_limit"] == 225_000_000
        assert create_kwargs["monthly_cost_limit"] == 0.0
        assert create_kwargs["daily_cost_limit"] == 0.0


class TestQuotaPolicyDataclassCostFields:
    """Cost budgets are first-class on the QuotaPolicy dataclass."""

    def test_dynamodb_round_trip(self):
        from decimal import Decimal

        from claude_code_with_bedrock.models import PolicyType, QuotaPolicy

        policy = QuotaPolicy(
            policy_type=PolicyType.DEFAULT,
            identifier="default",
            monthly_token_limit=0,
            monthly_cost_limit=50.0,
            daily_cost_limit=5.0,
        )
        item = policy.to_dynamodb_item()
        # DynamoDB rejects Python floats — budgets must be Decimal
        assert isinstance(item["monthly_cost_limit"], Decimal)
        assert isinstance(item["daily_cost_limit"], Decimal)

        loaded = QuotaPolicy.from_dynamodb_item(item)
        assert loaded.monthly_cost_limit == 50.0
        assert loaded.daily_cost_limit == 5.0

    def test_old_items_without_cost_attributes_default_to_zero(self):
        from claude_code_with_bedrock.models import PolicyType, QuotaPolicy

        item = QuotaPolicy(
            policy_type=PolicyType.USER,
            identifier="user@example.gov",
            monthly_token_limit=100_000_000,
        ).to_dynamodb_item()
        assert "monthly_cost_limit" not in item  # token-only items keep their shape

        loaded = QuotaPolicy.from_dynamodb_item(item)
        assert loaded.monthly_cost_limit == 0.0
        assert loaded.daily_cost_limit == 0.0

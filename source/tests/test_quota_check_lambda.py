# ABOUTME: Tests for the quota_check Lambda function's daily enforcement logic
# ABOUTME: Covers both env-var (ENABLE_FINEGRAINED_QUOTAS=false) and DynamoDB-backed paths

"""Tests for quota_check Lambda daily enforcement (block vs alert)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

LAMBDA_PATH = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "quota_check"
    / "index.py"
)


def _load_quota_check(env: dict) -> object:
    """Load the quota_check Lambda module fresh with the given environment.

    The module reads env vars at import time, so we must reload it after
    setting environment variables.
    """
    # Apply env vars before module import
    for key, value in env.items():
        os.environ[key] = value

    # Force a fresh import each time so module-level env reads take effect
    module_name = f"quota_check_index_{id(env)}"
    spec = importlib.util.spec_from_file_location(module_name, LAMBDA_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _build_event(email: str = "user@example.com", groups: list[str] | None = None) -> dict:
    claims: dict = {"email": email}
    if groups is not None:
        claims["groups"] = groups
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


def _parse(response: dict) -> dict:
    return json.loads(response["body"])


@pytest.fixture
def base_env():
    """Minimal env vars common to all tests."""
    return {
        "QUOTA_TABLE": "TestQuotaTable",
        "POLICIES_TABLE": "TestPoliciesTable",
        "MISSING_EMAIL_ENFORCEMENT": "block",
        "ERROR_HANDLING_MODE": "fail_closed",
    }


# ---------------------------------------------------------------------------
# Env-var path: ENABLE_FINEGRAINED_QUOTAS=false
# ---------------------------------------------------------------------------


class TestDailyEnforcementEnvVarPath:
    """ENABLE_FINEGRAINED_QUOTAS=false -> policy comes from env vars."""

    def _make_module(self, base_env, daily_mode: str):
        env = {
            **base_env,
            "ENABLE_FINEGRAINED_QUOTAS": "false",
            "MONTHLY_TOKEN_LIMIT": "1000",
            "DAILY_TOKEN_LIMIT": "100",
            "MONTHLY_ENFORCEMENT_MODE": "block",
            "DAILY_ENFORCEMENT_MODE": daily_mode,
        }
        return _load_quota_check(env)

    def _patch_usage_and_unblock(self, mod, daily_tokens: int, monthly_tokens: int = 0):
        mod.quota_table = MagicMock()
        # First call = unblock status (no item), second call = monthly usage
        mod.quota_table.get_item.side_effect = [
            {},  # no unblock entry
            {
                "Item": {
                    "total_tokens": monthly_tokens,
                    "daily_tokens": daily_tokens,
                    "daily_date": mod.datetime.now(mod.timezone.utc).strftime("%Y-%m-%d"),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_tokens": 0,
                }
            },
        ]

    def test_daily_block_mode_blocks_when_exceeded(self, base_env):
        mod = self._make_module(base_env, daily_mode="block")
        self._patch_usage_and_unblock(mod, daily_tokens=150)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "daily_exceeded"

    def test_daily_alert_mode_allows_when_exceeded(self, base_env):
        mod = self._make_module(base_env, daily_mode="alert")
        self._patch_usage_and_unblock(mod, daily_tokens=150)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "within_quota"

    def test_daily_block_mode_allows_under_limit(self, base_env):
        mod = self._make_module(base_env, daily_mode="block")
        self._patch_usage_and_unblock(mod, daily_tokens=50)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True


# ---------------------------------------------------------------------------
# DynamoDB path: ENABLE_FINEGRAINED_QUOTAS=true
# ---------------------------------------------------------------------------


class TestDailyEnforcementFineGrainedPath:
    """ENABLE_FINEGRAINED_QUOTAS=true -> policy comes from DynamoDB.

    These tests cover the bug where get_policy() did not include
    daily_enforcement_mode in its returned dict, causing daily block mode
    to be silently downgraded to alert.
    """

    def _make_module(self, base_env):
        env = {
            **base_env,
            "ENABLE_FINEGRAINED_QUOTAS": "true",
        }
        return _load_quota_check(env)

    def _setup_mocks(
        self,
        mod,
        policy_item: dict,
        daily_tokens: int,
        monthly_tokens: int = 0,
    ):
        # policies_table: user policy hit
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.return_value = {"Item": policy_item}

        # quota_table: no unblock, then monthly usage row
        mod.quota_table = MagicMock()
        mod.quota_table.get_item.side_effect = [
            {},  # unblock lookup
            {
                "Item": {
                    "total_tokens": monthly_tokens,
                    "daily_tokens": daily_tokens,
                    "daily_date": mod.datetime.now(mod.timezone.utc).strftime("%Y-%m-%d"),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_tokens": 0,
                }
            },
        ]

    def test_get_policy_returns_daily_enforcement_mode(self, base_env):
        """get_policy() must include daily_enforcement_mode from DynamoDB."""
        mod = self._make_module(base_env)
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.return_value = {
            "Item": {
                "policy_type": "user",
                "identifier": "user@example.com",
                "monthly_token_limit": 1000,
                "daily_token_limit": 100,
                "warning_threshold_80": 800,
                "warning_threshold_90": 900,
                "enforcement_mode": "block",
                "daily_enforcement_mode": "block",
                "enabled": True,
            }
        }

        policy = mod.get_policy("user", "user@example.com")
        assert policy is not None
        assert policy["daily_enforcement_mode"] == "block"

    def test_get_policy_defaults_daily_enforcement_mode_to_alert(self, base_env):
        """When DynamoDB item omits the field, default to 'alert'."""
        mod = self._make_module(base_env)
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.return_value = {
            "Item": {
                "policy_type": "user",
                "identifier": "user@example.com",
                "monthly_token_limit": 1000,
                "daily_token_limit": 100,
                "warning_threshold_80": 800,
                "warning_threshold_90": 900,
                "enforcement_mode": "block",
                "enabled": True,
                # daily_enforcement_mode intentionally omitted
            }
        }

        policy = mod.get_policy("user", "user@example.com")
        assert policy["daily_enforcement_mode"] == "alert"

    def test_finegrained_daily_block_mode_blocks_when_exceeded(self, base_env):
        """Regression: daily_enforcement_mode='block' from DynamoDB must block."""
        mod = self._make_module(base_env)
        self._setup_mocks(
            mod,
            policy_item={
                "policy_type": "user",
                "identifier": "user@example.com",
                "monthly_token_limit": 1000,
                "daily_token_limit": 100,
                "warning_threshold_80": 800,
                "warning_threshold_90": 900,
                "enforcement_mode": "block",
                "daily_enforcement_mode": "block",
                "enabled": True,
            },
            daily_tokens=150,
        )

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "daily_exceeded"

    def test_finegrained_daily_alert_mode_allows_when_exceeded(self, base_env):
        mod = self._make_module(base_env)
        self._setup_mocks(
            mod,
            policy_item={
                "policy_type": "user",
                "identifier": "user@example.com",
                "monthly_token_limit": 1000,
                "daily_token_limit": 100,
                "warning_threshold_80": 800,
                "warning_threshold_90": 900,
                "enforcement_mode": "block",
                "daily_enforcement_mode": "alert",
                "enabled": True,
            },
            daily_tokens=150,
        )

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "within_quota"

    def test_finegrained_missing_daily_mode_defaults_to_alert(self, base_env):
        """If the DynamoDB item omits daily_enforcement_mode, treat as 'alert'."""
        mod = self._make_module(base_env)
        self._setup_mocks(
            mod,
            policy_item={
                "policy_type": "user",
                "identifier": "user@example.com",
                "monthly_token_limit": 1000,
                "daily_token_limit": 100,
                "warning_threshold_80": 800,
                "warning_threshold_90": 900,
                "enforcement_mode": "block",
                "enabled": True,
                # daily_enforcement_mode missing
            },
            daily_tokens=150,
        )

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "within_quota"


# ---------------------------------------------------------------------------
# Contract tests: response schema validation
# ---------------------------------------------------------------------------


# Required keys in every quota_check response body
RESPONSE_REQUIRED_KEYS = {"allowed"}

# Keys expected when a quota policy exists and user is within quota
NORMAL_RESPONSE_KEYS = {"allowed", "reason", "enforcement_mode", "usage", "policy", "unblock_status", "message"}

# Valid values for 'reason' field
VALID_REASONS = {
    "within_quota",
    "monthly_exceeded",
    "daily_exceeded",
    "no_policy",
    "no_email",
    "unblocked",
    "missing_email_claim",
}

# Valid values for 'enforcement_mode' field
VALID_ENFORCEMENT_MODES = {"alert", "block", None}


class TestResponseSchemaContract:
    """Contract tests ensuring quota_check Lambda responses conform to expected schema.

    The credential-process binary parses these responses. If the schema changes,
    credential-process breaks silently (users get blocked or allowed incorrectly).
    These tests ensure both sides agree on the contract.
    """

    def _make_module(self, base_env, **overrides):
        env = {
            **base_env,
            "ENABLE_FINEGRAINED_QUOTAS": "false",
            "MONTHLY_TOKEN_LIMIT": "1000",
            "DAILY_TOKEN_LIMIT": "100",
            "MONTHLY_ENFORCEMENT_MODE": "block",
            "DAILY_ENFORCEMENT_MODE": "block",
            **overrides,
        }
        return _load_quota_check(env)

    def _patch_usage(self, mod, daily_tokens: int = 0, monthly_tokens: int = 0):
        mod.quota_table = MagicMock()
        mod.quota_table.get_item.side_effect = [
            {},  # unblock
            {
                "Item": {
                    "total_tokens": monthly_tokens,
                    "daily_tokens": daily_tokens,
                    "daily_date": mod.datetime.now(mod.timezone.utc).strftime("%Y-%m-%d"),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_tokens": 0,
                }
            },
        ]

    def test_response_is_valid_json_with_status_code(self, base_env):
        """Lambda returns dict with statusCode and JSON-parseable body."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        response = mod.lambda_handler(_build_event(), None)
        assert "statusCode" in response
        assert "body" in response
        assert isinstance(response["statusCode"], int)
        body = json.loads(response["body"])
        assert isinstance(body, dict)

    def test_allowed_response_has_required_keys(self, base_env):
        """When allowed=True, response includes all expected keys."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        for key in NORMAL_RESPONSE_KEYS:
            assert key in body, f"Missing key '{key}' in allowed response"

    def test_blocked_response_has_required_keys(self, base_env):
        """When allowed=False, response includes all expected keys."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=150)  # exceeds 100 limit

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        for key in NORMAL_RESPONSE_KEYS:
            assert key in body, f"Missing key '{key}' in blocked response"

    def test_reason_field_is_valid_enum(self, base_env):
        """'reason' field uses a known value."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["reason"] in VALID_REASONS, f"Unknown reason: {body['reason']}"

    def test_enforcement_mode_is_valid(self, base_env):
        """'enforcement_mode' is alert, block, or None."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["enforcement_mode"] in VALID_ENFORCEMENT_MODES

    def test_usage_summary_structure(self, base_env):
        """'usage' field contains expected token count keys."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=50, monthly_tokens=200)

        body = _parse(mod.lambda_handler(_build_event(), None))
        usage = body["usage"]
        assert usage is not None
        assert "monthly_tokens" in usage
        assert "monthly_limit" in usage
        assert "monthly_percent" in usage
        assert "daily_tokens" in usage
        assert "daily_limit" in usage

    def test_policy_field_structure(self, base_env):
        """'policy' field contains type and identifier."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        policy = body["policy"]
        assert policy is not None
        assert "type" in policy
        assert "identifier" in policy

    def test_unblock_status_structure(self, base_env):
        """'unblock_status' field contains is_unblocked boolean."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert "unblock_status" in body
        assert "is_unblocked" in body["unblock_status"]
        assert isinstance(body["unblock_status"]["is_unblocked"], bool)

    def test_message_field_is_string(self, base_env):
        """'message' field is always a human-readable string."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert isinstance(body["message"], str)
        assert len(body["message"]) > 0

    def test_monthly_exceeded_sets_reason_correctly(self, base_env):
        """Monthly limit exceeded returns reason='monthly_exceeded'."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, monthly_tokens=1500)  # exceeds 1000 limit

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "monthly_exceeded"

    def test_no_policy_response(self, base_env):
        """When MONTHLY_TOKEN_LIMIT=0 (disabled), returns no_policy."""
        mod = self._make_module(base_env, MONTHLY_TOKEN_LIMIT="0", DAILY_TOKEN_LIMIT="0")
        self._patch_usage(mod)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "no_policy"


class TestInputValidationContract:
    """Contract tests for input handling — ensures malformed requests don't crash."""

    def _make_module(self, base_env):
        env = {
            **base_env,
            "ENABLE_FINEGRAINED_QUOTAS": "false",
            "MONTHLY_TOKEN_LIMIT": "1000",
            "DAILY_TOKEN_LIMIT": "100",
            "MONTHLY_ENFORCEMENT_MODE": "block",
            "DAILY_ENFORCEMENT_MODE": "block",
        }
        return _load_quota_check(env)

    def test_missing_email_claim(self, base_env):
        """Request with no email in JWT claims returns structured response."""
        mod = self._make_module(base_env)
        event = {"requestContext": {"authorizer": {"jwt": {"claims": {}}}}}

        response = mod.lambda_handler(event, None)
        assert response["statusCode"] == 200
        body = _parse(response)
        assert "allowed" in body
        assert body.get("reason") in ("missing_email_claim", "missing_identity")

    def test_missing_authorizer_context(self, base_env):
        """Request with no authorizer context does not crash."""
        mod = self._make_module(base_env)
        event = {"requestContext": {}}

        response = mod.lambda_handler(event, None)
        assert response["statusCode"] == 200
        body = _parse(response)
        assert "allowed" in body

    def test_empty_event(self, base_env):
        """Completely empty event does not crash the Lambda."""
        mod = self._make_module(base_env)

        response = mod.lambda_handler({}, None)
        assert response["statusCode"] == 200
        body = _parse(response)
        assert "allowed" in body

    def test_missing_email_blocked_by_default(self, base_env):
        """Missing email defaults to blocked (fail-closed security)."""
        env = {**base_env, "MISSING_EMAIL_ENFORCEMENT": "block"}
        mod = _load_quota_check(
            {
                **env,
                "ENABLE_FINEGRAINED_QUOTAS": "false",
                "MONTHLY_TOKEN_LIMIT": "1000",
                "DAILY_TOKEN_LIMIT": "100",
                "MONTHLY_ENFORCEMENT_MODE": "block",
                "DAILY_ENFORCEMENT_MODE": "block",
            }
        )
        event = {"requestContext": {"authorizer": {"jwt": {"claims": {}}}}}

        body = _parse(mod.lambda_handler(event, None))
        assert body["allowed"] is False


class TestIdcUsernameIdentity:
    """Tests for IAM Identity Center username-based identity resolution (#592 feedback)."""

    @pytest.fixture
    def base_env(self):
        return {
            "AWS_DEFAULT_REGION": "us-east-1",
            "QUOTA_TABLE_NAME": "test-table",
            "ENABLE_FINEGRAINED_QUOTAS": "false",
            "MONTHLY_TOKEN_LIMIT": "1000000",
            "DAILY_TOKEN_LIMIT": "100000",
            "MONTHLY_ENFORCEMENT_MODE": "warn",
            "DAILY_ENFORCEMENT_MODE": "warn",
            "MISSING_EMAIL_ENFORCEMENT": "allow",
        }

    def test_email_session_name_resolves(self, base_env):
        """Standard case: IDC username is an email address."""
        mod = _load_quota_check(base_env)
        event = {
            "requestContext": {
                "authorizer": {"jwt": {"claims": {}}},
                "identity": {
                    "caller": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockDeveloper_abc123/user@company.com"
                },
            }
        }
        body = _parse(mod.lambda_handler(event, None))
        # Should resolve identity and not return missing_identity
        assert body.get("reason") != "missing_identity"

    def test_non_email_idc_username_resolves(self, base_env):
        """IDC username without @ (e.g. 'akshaya.claude') should still resolve."""
        mod = _load_quota_check(base_env)
        event = {
            "requestContext": {
                "authorizer": {"jwt": {"claims": {}}},
                "identity": {
                    "caller": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockDeveloper_abc123/akshaya.claude"
                },
            }
        }
        body = _parse(mod.lambda_handler(event, None))
        # Should resolve identity (not missing_identity)
        assert body.get("reason") != "missing_identity"

    def test_non_sso_role_without_email_does_not_resolve(self, base_env):
        """Non-SSO role without @ should NOT be treated as identity."""
        env = {**base_env, "MISSING_EMAIL_ENFORCEMENT": "block"}
        mod = _load_quota_check(env)
        event = {
            "requestContext": {
                "authorizer": {"jwt": {"claims": {}}},
                "identity": {"caller": "arn:aws:sts::123456789012:assumed-role/CustomRole/session123"},
            }
        }
        body = _parse(mod.lambda_handler(event, None))
        # Non-SSO role without email should be blocked
        assert body.get("reason") == "missing_identity"
        assert body["allowed"] is False


class TestCostBasedEnforcement:
    """Tests that cost-based quota enforcement reads the correct DDB attributes.

    Regression tests for issue #746: monthly cost enforcement was broken because:
    1. get_user_usage() didn't include cost fields in its return dict
    2. get_policy() didn't include monthly_cost_limit/daily_cost_limit
    3. The lookup key was 'cost_usd' but DDB attribute is 'estimated_cost'
    """

    def _make_module(self, base_env):
        env = {
            **base_env,
            "ENABLE_FINEGRAINED_QUOTAS": "true",
        }
        return _load_quota_check(env)

    def _patch_tables(
        self,
        mod,
        estimated_cost: float,
        monthly_cost_limit: float,
        daily_cost_usd: float = 0,
        daily_cost_limit: float = 0,
    ):
        """Mock both quota_table and policies_table with correct call sequence."""
        from datetime import datetime, timezone

        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        mod.quota_table = MagicMock()
        # Call 1: get_unblock_status -> no unblock
        # Call 2: get_user_usage -> usage with estimated_cost
        mod.quota_table.get_item.side_effect = [
            {},  # unblock check: no item
            {
                "Item": {
                    "total_tokens": 100000,
                    "daily_tokens": 1000,
                    "daily_date": current_date,
                    "input_tokens": 60000,
                    "output_tokens": 40000,
                    "cache_tokens": 0,
                    "estimated_cost": estimated_cost,
                    "daily_cost_usd": daily_cost_usd,
                }
            },
        ]

        mod.policies_table = MagicMock()
        mod.policies_table.get_item.return_value = {
            "Item": {
                "pk": "POLICY#default#default",
                "sk": "CURRENT",
                "policy_type": "default",
                "identifier": "default",
                "monthly_token_limit": 0,
                "monthly_cost_limit": monthly_cost_limit,
                "daily_cost_limit": daily_cost_limit,
                "enforcement_mode": "block",
                "daily_enforcement_mode": "block",
                "enabled": True,
            }
        }

    def test_monthly_cost_blocks_when_exceeded(self, base_env):
        """When estimated_cost exceeds monthly_cost_limit, access must be denied."""
        mod = self._make_module(base_env)
        self._patch_tables(mod, estimated_cost=95.0, monthly_cost_limit=90.0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False, (
            "Monthly cost enforcement failed: estimated_cost ($95) > limit ($90) but access was allowed. "
            "Check that get_user_usage includes 'estimated_cost' and get_policy includes 'monthly_cost_limit'."
        )
        assert body["reason"] == "monthly_cost_exceeded"

    def test_monthly_cost_allows_when_within_limit(self, base_env):
        """When estimated_cost is below monthly_cost_limit, access is granted."""
        mod = self._make_module(base_env)
        self._patch_tables(mod, estimated_cost=45.0, monthly_cost_limit=90.0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True

    def test_daily_cost_blocks_when_exceeded(self, base_env):
        """When daily_cost_usd exceeds daily_cost_limit, access must be denied."""
        mod = self._make_module(base_env)
        self._patch_tables(
            mod, estimated_cost=10.0, monthly_cost_limit=90.0, daily_cost_usd=12.0, daily_cost_limit=10.0
        )

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "daily_cost_exceeded"


class TestGroupPolicyRestrictiveness:
    """Most-restrictive group selection must account for cost-based policies.

    Regression: selection was min() by raw monthly_token_limit, which ignored
    cost budgets entirely AND sorted a cost-only policy (token limit 0) as
    the most restrictive of any token policy.
    """

    def _cost_policy(self, ident: str, monthly_cost: float, daily_cost: float = 0) -> dict:
        return {
            "policy_type": "group",
            "identifier": ident,
            "monthly_token_limit": 0,
            "daily_token_limit": None,
            "monthly_cost_limit": monthly_cost,
            "daily_cost_limit": daily_cost,
            "warning_threshold_80": 0,
            "warning_threshold_90": 0,
            "enforcement_mode": "block",
            "daily_enforcement_mode": "alert",
            "enabled": True,
        }

    def _token_policy(self, ident: str, monthly_tokens: int) -> dict:
        return {
            "policy_type": "group",
            "identifier": ident,
            "monthly_token_limit": monthly_tokens,
            "daily_token_limit": None,
            "monthly_cost_limit": 0,
            "daily_cost_limit": 0,
            "warning_threshold_80": 0,
            "warning_threshold_90": 0,
            "enforcement_mode": "block",
            "daily_enforcement_mode": "alert",
            "enabled": True,
        }

    def test_lowest_cost_budget_wins_among_cost_policies(self):
        mod = _load_quota_check({"ENABLE_FINEGRAINED_QUOTAS": "true"})
        policies = [
            self._cost_policy("engineering", 500),
            self._cost_policy("interns", 50),
            self._cost_policy("research", 200),
        ]
        chosen = min(policies, key=mod.policy_restrictiveness_key)
        assert chosen["identifier"] == "interns"

    def test_zero_token_limit_is_not_most_restrictive(self):
        """A policy with token limit 0 (no token limit) must not beat a real limit."""
        mod = _load_quota_check({"ENABLE_FINEGRAINED_QUOTAS": "true"})
        policies = [
            self._token_policy("engineering", 100_000_000),
            self._token_policy("unlimited", 0),
        ]
        chosen = min(policies, key=mod.policy_restrictiveness_key)
        assert chosen["identifier"] == "engineering"

    def test_daily_cost_breaks_monthly_tie(self):
        mod = _load_quota_check({"ENABLE_FINEGRAINED_QUOTAS": "true"})
        policies = [
            self._cost_policy("loose", 200, daily_cost=0),
            self._cost_policy("tight", 200, daily_cost=20),
        ]
        chosen = min(policies, key=mod.policy_restrictiveness_key)
        assert chosen["identifier"] == "tight"

    def test_resolve_quota_for_user_selects_lowest_cost_group(self):
        """End-to-end through resolve_quota_for_user with mocked policy reads."""
        mod = _load_quota_check({"ENABLE_FINEGRAINED_QUOTAS": "true", "MONTHLY_TOKEN_LIMIT": "0", "MONTHLY_COST_LIMIT_USD": "0"})
        by_group = {
            "engineering": self._cost_policy("engineering", 500),
            "interns": self._cost_policy("interns", 50),
        }
        mod.get_policy = lambda ptype, ident: by_group.get(ident) if ptype == "group" else None

        policy = mod.resolve_quota_for_user("dev@x.com", ["engineering", "interns"])

        assert policy is not None
        assert policy["identifier"] == "interns"
        assert policy["monthly_cost_limit"] == 50


class TestGroupRecording:
    """quota_check must persist JWT group memberships for the monitor.

    quota_monitor's data source (metrics) has no group information — the
    GROUPS#CURRENT records written here are its only way to resolve group
    policies. Regression: groups were never persisted anywhere, so the
    monitor hard-coded groups=[] and group policies were never alerted.
    """

    def _run(self, env: dict, event: dict):
        mod = _load_quota_check(env)
        mod.quota_table = MagicMock()
        mod.quota_table.get_item.return_value = {}
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.return_value = {}
        mod.lambda_handler(event, None)
        return mod

    def _groups_write_calls(self, mod):
        return [
            c
            for c in mod.quota_table.update_item.call_args_list
            if c.kwargs.get("Key", {}).get("sk") == "GROUPS#CURRENT"
        ]

    def test_jwt_groups_recorded(self):
        mod = self._run(
            {"ENABLE_FINEGRAINED_QUOTAS": "false", "MONTHLY_TOKEN_LIMIT": "40000000"},
            _build_event(email="dev@x.com", groups=["engineering", "ai-team"]),
        )

        writes = self._groups_write_calls(mod)
        assert len(writes) == 1
        assert writes[0].kwargs["Key"]["pk"] == "USER#dev@x.com"
        values = writes[0].kwargs["ExpressionAttributeValues"]
        assert values[":groups"] == ["ai-team", "engineering"]  # sorted
        assert values[":email"] == "dev@x.com"

    def test_jwt_empty_groups_still_recorded(self):
        """An empty list must be written so group removals propagate."""
        mod = self._run(
            {"ENABLE_FINEGRAINED_QUOTAS": "false", "MONTHLY_TOKEN_LIMIT": "40000000"},
            _build_event(email="dev@x.com", groups=[]),
        )
        writes = self._groups_write_calls(mod)
        assert len(writes) == 1
        assert writes[0].kwargs["ExpressionAttributeValues"][":groups"] == []

    def test_iam_path_does_not_overwrite_groups(self):
        """The IDC/IAM path has no group info — it must not erase records."""
        event = {
            "requestContext": {
                "identity": {
                    "userArn": "arn:aws-us-gov:sts::123456789012:assumed-role/AWSReservedSSO_x/dev@x.com"
                }
            }
        }
        mod = self._run({"ENABLE_FINEGRAINED_QUOTAS": "false", "MONTHLY_TOKEN_LIMIT": "40000000"}, event)
        assert self._groups_write_calls(mod) == []

    def test_write_failure_does_not_fail_the_check(self):
        mod = _load_quota_check({"ENABLE_FINEGRAINED_QUOTAS": "false", "MONTHLY_TOKEN_LIMIT": "40000000"})
        mod.quota_table = MagicMock()
        mod.quota_table.get_item.return_value = {}
        mod.quota_table.update_item.side_effect = Exception("throttled")
        mod.policies_table = MagicMock()

        response = mod.lambda_handler(_build_event(groups=["engineering"]), None)

        body = _parse(response)
        assert body["allowed"] is True

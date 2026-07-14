# ABOUTME: Tests for the AMP metrics backend (GovCloud): partition-based backend
# ABOUTME: resolution, deploy stack ordering, collector config generation, quota queries.

"""GovCloud metrics backend regression tests.

CloudWatch's OTLP ingestion (/v1/metrics) and PromQL (/api/v1/query) routes
exist only in commercial regions — in GovCloud they 404. The 'amp' backend
exports to an Amazon Managed Service for Prometheus workspace instead (plus
EMF for the CloudWatch dashboard). These tests pin:

- Profile.effective_metrics_backend partition resolution and field round-trip
- deploy stack scheduling (amp workspace before auth/dashboard/quota)
- sidecar collector config generation for the -amp template variants
- quota_monitor's backend-specific endpoint, SigV4 service, and PromQL shape
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

from rich.console import Console

from claude_code_with_bedrock.cli.commands.deploy import DeployCommand
from claude_code_with_bedrock.cli.commands.package import PackageCommand
from claude_code_with_bedrock.config import Profile

QUOTA_MONITOR_PATH = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "quota_monitor"
    / "index.py"
)


def _profile(**overrides) -> Profile:
    defaults = {
        "name": "govtest",
        "provider_domain": "auth.example.com",
        "client_id": "client-123",
        "credential_storage": "session",
        "aws_region": "us-gov-west-1",
        "identity_pool_name": "gov-pool",
        "auth_type": "oidc",
        "monitoring_enabled": True,
        "monitoring_mode": "sidecar",
        "analytics_enabled": False,
    }
    defaults.update(overrides)
    return Profile(**defaults)


class TestEffectiveMetricsBackend:
    def test_auto_resolves_amp_in_govcloud(self):
        assert _profile(aws_region="us-gov-west-1").effective_metrics_backend == "amp"
        assert _profile(aws_region="us-gov-east-1").effective_metrics_backend == "amp"

    def test_auto_resolves_cloudwatch_in_commercial(self):
        assert _profile(aws_region="us-east-1").effective_metrics_backend == "cloudwatch-otlp"
        assert _profile(aws_region="eu-west-1").effective_metrics_backend == "cloudwatch-otlp"

    def test_explicit_override_wins(self):
        assert _profile(aws_region="us-east-1", metrics_backend="amp").effective_metrics_backend == "amp"
        assert (
            _profile(aws_region="us-gov-west-1", metrics_backend="cloudwatch-otlp").effective_metrics_backend
            == "cloudwatch-otlp"
        )

    def test_old_profile_without_field_resolves_by_partition(self):
        """Backward compat: profiles saved before metrics_backend existed."""
        data = _profile().to_dict()
        data.pop("metrics_backend")
        for key in ("amp_workspace_id", "amp_workspace_arn", "amp_remote_write_url", "amp_query_url"):
            data.pop(key)
        loaded = Profile.from_dict(data)
        assert loaded.effective_metrics_backend == "amp"

    def test_amp_fields_round_trip(self):
        p = _profile(
            amp_workspace_id="ws-123",
            amp_workspace_arn="arn:aws-us-gov:aps:us-gov-west-1:123456789012:workspace/ws-123",
            amp_remote_write_url="https://aps-workspaces.us-gov-west-1.amazonaws.com/workspaces/ws-123/api/v1/remote_write",
            amp_query_url="https://aps-workspaces.us-gov-west-1.amazonaws.com/workspaces/ws-123/api/v1/query",
        )
        loaded = Profile.from_dict(p.to_dict())
        assert loaded.amp_workspace_id == "ws-123"
        assert loaded.amp_workspace_arn == p.amp_workspace_arn
        assert loaded.amp_remote_write_url == p.amp_remote_write_url
        assert loaded.amp_query_url == p.amp_query_url


class TestDeployStackSelection:
    def _stacks(self, profile) -> list[str]:
        cmd = DeployCommand()
        return [s for s, _ in cmd._select_full_deploy_stacks(profile, Console(quiet=True))]

    def test_sidecar_govcloud_schedules_amp_before_auth_and_dashboard(self):
        stacks = self._stacks(_profile())
        assert "amp" in stacks, f"amp workspace stack missing from {stacks}"
        # The auth stack scopes user IAM (aps:RemoteWrite) to the workspace ARN
        # and the dashboard/quota stacks read its outputs — it must come first.
        assert stacks.index("amp") < stacks.index("auth")
        assert stacks.index("amp") < stacks.index("dashboard")

    def test_sidecar_commercial_has_no_amp_stack(self):
        stacks = self._stacks(_profile(aws_region="us-east-1"))
        assert "amp" not in stacks
        assert "dashboard" in stacks

    def test_central_mode_has_no_amp_stack(self):
        stacks = self._stacks(_profile(monitoring_mode="central", monitoring_config={"create_vpc": False}))
        assert "amp" not in stacks

    def test_sidecar_govcloud_quota_follows_amp(self):
        stacks = self._stacks(_profile(quota_monitoring_enabled=True))
        assert stacks.index("amp") < stacks.index("quota")


class TestCollectorConfigGeneration:
    AMP_URL = "https://aps-workspaces.us-gov-west-1.amazonaws.com/workspaces/ws-abc/api/v1/remote_write"

    def _generate(self, template_name: str, **kwargs) -> str:
        cmd = PackageCommand()
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            cmd._generate_collector_config(
                output_dir=out,
                template_name=template_name,
                region="us-gov-west-1",
                amp_remote_write_url=self.AMP_URL,
                **kwargs,
            )
            return (out / "collector-config.yaml").read_text(encoding="utf-8")

    def test_amp_oidc_config_renders(self):
        content = self._generate("collector-config-amp.yaml")
        assert self.AMP_URL in content, "remote-write URL must be substituted"
        assert "${AMP_REMOTE_WRITE_URL}" not in content
        assert "${REGION}" not in content
        assert 'service: "aps"' in content, "sigv4auth must sign for the aps service"
        assert "prometheusremotewrite:" in content
        assert "awsemf:" in content, "EMF dual-export feeds the CloudWatch dashboard"
        assert "namespace: ClaudeCode" in content

    def test_amp_oidc_deltatocumulative_only_in_amp_pipeline(self):
        """Prometheus remote-write needs cumulative counters, but EMF must keep
        the delta datapoints (converting both would double-count dashboards)."""
        content = self._generate("collector-config-amp.yaml")
        assert "processors: [attributes, deltatocumulative, batch/metrics]" in content
        assert "processors: [attributes, batch/metrics]" in content

    def test_amp_idc_config_renders_static_identity(self):
        content = self._generate(
            "collector-config-amp-idc.yaml",
            idc_user_email="user@example.gov",
            otel_resource_attributes="department=eng,team.id=platform",
        )
        assert self.AMP_URL in content
        assert "user@example.gov" in content
        assert "${USER_EMAIL}" not in content
        assert "processors: [resource/identity, deltatocumulative, batch/metrics]" in content

    def test_amp_templates_keep_metric_names_stable(self):
        """add_metric_suffixes must stay off: quota queries and Grafana
        dashboards depend on claude_code_token_usage (no _total/unit suffix)."""
        for template in ("collector-config-amp.yaml", "collector-config-amp-idc.yaml"):
            kwargs = {"idc_user_email": "u@e.gov"} if template.endswith("idc.yaml") else {}
            content = self._generate(template, **kwargs)
            assert "add_metric_suffixes: false" in content, template


def _load_quota_monitor(env: dict) -> object:
    """Load the quota_monitor Lambda module fresh with the given environment."""
    base = {
        "AWS_DEFAULT_REGION": "us-gov-west-1",
        "QUOTA_TABLE": "TestQuotaTable",
        "POLICIES_TABLE": "TestPoliciesTable",
        "SNS_TOPIC_ARN": "arn:aws-us-gov:sns:us-gov-west-1:123456789012:test-alerts",
    }
    base.update(env)
    old = {k: os.environ.get(k) for k in ("METRICS_BACKEND", "PROMQL_ENDPOINT", "METRICS_REGION")}
    for key, value in base.items():
        os.environ[key] = value
    try:
        module_name = f"quota_monitor_backend_{abs(hash(frozenset(base.items())))}"
        spec = importlib.util.spec_from_file_location(module_name, QUOTA_MONITOR_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestQuotaMonitorBackends:
    AMP_QUERY_URL = "https://aps-workspaces.us-gov-west-1.amazonaws.com/workspaces/ws-abc/api/v1/query"

    def test_cloudwatch_backend_defaults(self):
        mod = _load_quota_monitor(
            {"METRICS_REGION": "us-east-1", "METRICS_BACKEND": "cloudwatch", "PROMQL_ENDPOINT": ""}
        )
        assert mod.SIGNING_SERVICE == "monitoring"
        assert mod.PROMQL_ENDPOINT == "https://monitoring.us-east-1.amazonaws.com/api/v1/query"
        q = mod._usage_queries(900)
        # Delta temporality -> sum_over_time over the OTel-native dotted names.
        assert "sum_over_time" in q["total"]
        assert '"claude_code.token.usage"' in q["total"]
        assert q["email_label"] == "user.email"

    def test_amp_backend_endpoint_service_and_queries(self):
        mod = _load_quota_monitor(
            {
                "METRICS_REGION": "us-gov-west-1",
                "METRICS_BACKEND": "amp",
                "PROMQL_ENDPOINT": self.AMP_QUERY_URL,
            }
        )
        assert mod.SIGNING_SERVICE == "aps"
        assert mod.PROMQL_ENDPOINT == self.AMP_QUERY_URL
        q = mod._usage_queries(900)
        # Cumulative (deltatocumulative in the collector) -> increase(), and
        # Prometheus-normalized names/labels (dots -> underscores).
        assert "increase(claude_code_token_usage[900s])" in q["total"]
        assert q["email_label"] == "user_email"
        assert "user_email, type, model" in q["type_model"]

    def test_amp_backend_skips_cowork_cloudwatch_queries(self):
        """CoWork MetricFilter metrics live only in CloudWatch — on the AMP
        backend the fetch must return before attempting those queries."""
        mod = _load_quota_monitor(
            {
                "METRICS_REGION": "us-gov-west-1",
                "METRICS_BACKEND": "amp",
                "PROMQL_ENDPOINT": self.AMP_QUERY_URL,
            }
        )
        calls = []

        def fake_query(query, time_param=None):
            calls.append(query)
            return []

        mod._promql_query = fake_query
        mod.fetch_usage_from_promql()
        assert calls, "usage queries must run"
        assert not any("ClaudeCoWork" in q for q in calls), "CoWork queries must be skipped on AMP"


class TestAmpWorkspaceTemplate:
    def test_tag_values_use_aps_allowed_charset(self):
        """APS rejects tag values outside letters/digits/space/_.:/=+-@ —
        a parenthesized Purpose tag failed workspace creation outright."""
        template = Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "amp-workspace.yaml"
        import re

        import yaml

        class CFNLoader(yaml.SafeLoader):
            pass

        def _cfn(loader, suffix, node):  # noqa: ARG001 - yaml constructor signature
            if isinstance(node, yaml.ScalarNode):
                return loader.construct_scalar(node)
            if isinstance(node, yaml.SequenceNode):
                return loader.construct_sequence(node)
            return loader.construct_mapping(node)

        CFNLoader.add_multi_constructor("!", _cfn)
        doc = yaml.load(template.read_text(encoding="utf-8"), Loader=CFNLoader)
        tags = doc["Resources"]["Workspace"]["Properties"].get("Tags", [])
        assert tags, "workspace should carry an identifying tag"
        allowed = re.compile(r"^[\w\s_.:/=+\-@]*$")
        for tag in tags:
            for field in ("Key", "Value"):
                assert allowed.match(tag[field]), f"APS-invalid character in tag {field}: {tag[field]!r}"


class TestDashboardTemplateSelection:
    def test_amp_backend_uses_emf_dashboard_template(self):
        """deploy selects the classic-widget dashboard for the AMP backend —
        the PromQL widget template cannot render in GovCloud."""
        emf_template = (
            Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "claude-code-dashboard-emf.yaml"
        )
        assert emf_template.exists()
        content = emf_template.read_text(encoding="utf-8")
        # Widgets must query the EMF namespace via Metrics Insights, not PromQL.
        assert 'FROM SCHEMA(\\"ClaudeCode\\"' in content
        assert '"language": "PromQL"' not in content

# Grafana Dashboard for the AMP Metrics Backend

`claude-code-amp-dashboard.json` is an importable Grafana dashboard for
deployments using the **AMP metrics backend** (GovCloud sidecar mode, where
CloudWatch has no OTLP ingestion or PromQL support). It mirrors the panels of
the CloudWatch dashboards over the full-cardinality metrics stored in the
Amazon Managed Service for Prometheus workspace.

Grafana is **not** deployed by `ccwb deploy` — Amazon Managed Grafana is
available in the AWS GovCloud (US) Regions but does not support
CloudFormation there, so the workspace is a one-time manual setup. The
dashboard JSON below removes the authoring work: import it and you're done.

## One-time setup (Amazon Managed Grafana)

1. **Create an AMG workspace** (console → Amazon Managed Grafana → Create
   workspace). Choose AWS IAM Identity Center (or SAML) for authentication.
   Grant the workspace's service role access to Amazon Managed Service for
   Prometheus (the console's "data sources" checkbox does this for you).
2. **Add the AMP data source** in Grafana: Administration → Data sources →
   Add → *Amazon Managed Service for Prometheus*. Select your region and the
   Claude Code workspace (its ID is in the ccwb profile as
   `amp_workspace_id`, or in the `<pool>-amp` stack outputs). SigV4 auth is
   preconfigured for this data source type.
3. **Import the dashboard**: Dashboards → New → Import → upload
   `claude-code-amp-dashboard.json` → when prompted, select the data source
   from step 2.

Self-hosted Grafana OSS/Enterprise works too: enable the `sigv4` auth feature
(`AWS_SDK_LOAD_CONFIG=true`, sigv4 enabled in grafana.ini), add a *Prometheus*
data source pointing at the workspace query URL (`amp_query_url` in the ccwb
profile) with SigV4 auth, and import the same JSON.

## Notes on the data model

- Metric names are Prometheus-normalized by the collector's
  `prometheusremotewrite` exporter: `claude_code.token.usage` →
  `claude_code_token_usage` (suffixing is disabled, so no `_total`).
- Label names likewise: `user.email` → `user_email`, `team.id` → `team_id`.
- Counters are CUMULATIVE in AMP (the sidecar's `deltatocumulative`
  processor converts Claude Code's delta export), so panels aggregate with
  `increase()`. Collector restarts appear as counter resets, which
  `increase()` handles.
- Bedrock API health (throttles/errors) is not in AMP — those are
  `AWS/Bedrock` CloudWatch metrics; see the CloudWatch dashboard, or add a
  CloudWatch data source in Grafana if you want them side by side.

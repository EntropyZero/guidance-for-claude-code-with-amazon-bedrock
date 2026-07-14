package main

import (
	"bytes"
	"os"
	"strings"
	"testing"

	"ccwb-go/internal/quota"
)

// TestPrintQuotaWarning_AtThreshold verifies warning is emitted at 80%+ usage.
func TestPrintQuotaWarning_AtThreshold(t *testing.T) {
	tests := []struct {
		name           string
		monthlyPercent float64
		dailyPercent   float64
		expectWarning  bool
	}{
		{"below_threshold", 50, 50, false},
		{"monthly_at_80", 80, 50, true},
		{"daily_at_80", 50, 80, true},
		{"both_over", 95, 120, true},
		{"daily_over_100", 30, 451.9, true},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			qr := &quota.Result{
				Allowed: true,
				Reason:  "within_limits",
				Usage: map[string]interface{}{
					"monthly_percent": tc.monthlyPercent,
					"daily_percent":   tc.dailyPercent,
					"monthly_tokens":  float64(9000000),
					"monthly_limit":   float64(40000000),
					"daily_tokens":    float64(9000000),
					"daily_limit":     float64(2000000),
				},
			}

			// Capture stderr
			oldStderr := os.Stderr
			r, w, _ := os.Pipe()
			os.Stderr = w

			printQuotaWarning(qr)

			w.Close()
			var buf bytes.Buffer
			buf.ReadFrom(r)
			os.Stderr = oldStderr

			output := buf.String()
			hasWarning := strings.Contains(output, "QUOTA WARNING")

			if tc.expectWarning && !hasWarning {
				t.Errorf("expected QUOTA WARNING in stderr, got: %q", output)
			}
			if !tc.expectWarning && hasWarning {
				t.Errorf("did not expect QUOTA WARNING, but got: %q", output)
			}
		})
	}
}

// TestPrintQuotaWarning_StdoutSacred verifies that printQuotaWarning NEVER writes to stdout.
// stdout must remain exclusively for credential JSON (credential-flow.md rule).
func TestPrintQuotaWarning_StdoutSacred(t *testing.T) {
	qr := &quota.Result{
		Allowed: true,
		Reason:  "within_limits",
		Usage: map[string]interface{}{
			"monthly_percent": float64(95),
			"daily_percent":   float64(451.9),
			"monthly_tokens":  float64(38000000),
			"monthly_limit":   float64(40000000),
			"daily_tokens":    float64(9000000),
			"daily_limit":     float64(2000000),
		},
	}

	// Capture stdout
	oldStdout := os.Stdout
	rOut, wOut, _ := os.Pipe()
	os.Stdout = wOut

	// Capture stderr (to suppress output)
	oldStderr := os.Stderr
	_, wErr, _ := os.Pipe()
	os.Stderr = wErr

	printQuotaWarning(qr)

	wOut.Close()
	wErr.Close()
	var stdoutBuf bytes.Buffer
	stdoutBuf.ReadFrom(rOut)
	os.Stdout = oldStdout
	os.Stderr = oldStderr

	if stdoutBuf.Len() > 0 {
		t.Errorf("printQuotaWarning wrote to stdout (violates credential-flow rule): %q", stdoutBuf.String())
	}
}

// TestPrintQuotaWarning_NilUsage verifies no panic on nil usage.
func TestPrintQuotaWarning_NilUsage(t *testing.T) {
	qr := &quota.Result{Allowed: true, Reason: "within_limits", Usage: nil}
	// Should not panic
	printQuotaWarning(qr)
}

// TestPrintQuotaWarning_EmptyUsage verifies no warning on empty usage map.
func TestPrintQuotaWarning_EmptyUsage(t *testing.T) {
	qr := &quota.Result{Allowed: true, Reason: "within_limits", Usage: map[string]interface{}{}}

	oldStderr := os.Stderr
	r, w, _ := os.Pipe()
	os.Stderr = w

	printQuotaWarning(qr)

	w.Close()
	var buf bytes.Buffer
	buf.ReadFrom(r)
	os.Stderr = oldStderr

	if strings.Contains(buf.String(), "QUOTA WARNING") {
		t.Error("empty usage should not produce warning")
	}
}

// TestPrintQuotaUsageLines_CostMode verifies the cost-mode display: no
// "0 / 0 tokens" lines (token limits are 0 in cost mode) and dollar-
// denominated spend lines rendered from the quota API's cost fields.
func TestPrintQuotaUsageLines_CostMode(t *testing.T) {
	usage := map[string]interface{}{
		"monthly_percent":      float64(85),
		"monthly_tokens":       float64(12000000),
		"monthly_limit":        float64(0), // cost mode: token limits disabled
		"daily_tokens":         float64(400000),
		"monthly_cost":         float64(42.50),
		"monthly_cost_limit":   float64(50.0),
		"monthly_cost_percent": float64(85),
		"daily_cost":           float64(3.10),
		"daily_cost_limit":     float64(5.0),
		"daily_cost_percent":   float64(62),
	}

	oldStderr := os.Stderr
	r, w, _ := os.Pipe()
	os.Stderr = w

	printQuotaUsageLines(usage, 85, 62)

	w.Close()
	var buf bytes.Buffer
	buf.ReadFrom(r)
	os.Stderr = oldStderr
	output := buf.String()

	if !strings.Contains(output, "Monthly spend: $42.50 / $50.00 (85.0%)") {
		t.Errorf("expected monthly spend line, got: %q", output)
	}
	if !strings.Contains(output, "Daily spend: $3.10 / $5.00 (62.0%)") {
		t.Errorf("expected daily spend line, got: %q", output)
	}
	if strings.Contains(output, "tokens") {
		t.Errorf("token lines must not render when token limits are 0, got: %q", output)
	}
}

// TestPrintQuotaUsageLines_TokenMode verifies token deployments are unchanged:
// token lines render, no spend lines appear without cost limits.
func TestPrintQuotaUsageLines_TokenMode(t *testing.T) {
	usage := map[string]interface{}{
		"monthly_tokens": float64(9000000),
		"monthly_limit":  float64(40000000),
		"daily_tokens":   float64(900000),
		"daily_limit":    float64(2000000),
		"monthly_cost":   float64(12.34), // spend reported but no limit set
	}

	oldStderr := os.Stderr
	r, w, _ := os.Pipe()
	os.Stderr = w

	printQuotaUsageLines(usage, 22.5, 45)

	w.Close()
	var buf bytes.Buffer
	buf.ReadFrom(r)
	os.Stderr = oldStderr
	output := buf.String()

	if !strings.Contains(output, "Monthly: 9,000,000 / 40,000,000 tokens (22.5%)") {
		t.Errorf("expected monthly token line, got: %q", output)
	}
	if !strings.Contains(output, "Daily: 900,000 / 2,000,000 tokens (45.0%)") {
		t.Errorf("expected daily token line, got: %q", output)
	}
	if strings.Contains(output, "spend") {
		t.Errorf("spend lines must not render without cost limits, got: %q", output)
	}
}

// TestBuildQuotaHTML_CostMode verifies the browser notification renders
// dollar values (not "0 / 0" token counts) for cost-denominated policies.
func TestBuildQuotaHTML_CostMode(t *testing.T) {
	usage := map[string]interface{}{
		"monthly_percent":      float64(85),
		"monthly_limit":        float64(0),
		"monthly_cost":         float64(42.50),
		"monthly_cost_limit":   float64(50.0),
		"monthly_cost_percent": float64(85),
		"daily_cost":           float64(3.10),
		"daily_cost_limit":     float64(5.0),
		"daily_percent":        float64(62),
	}

	html := buildQuotaHTML(usage, "Approaching your monthly budget", false)

	if !strings.Contains(html, "$42.50 / $50.00") {
		t.Errorf("expected monthly spend in HTML, got: %q", html)
	}
	if !strings.Contains(html, "$3.10 / $5.00") {
		t.Errorf("expected daily spend section in HTML, got: %q", html)
	}
	if strings.Contains(html, "0 / 0") {
		t.Errorf("cost mode must not render 0 / 0 token counts, got: %q", html)
	}
}

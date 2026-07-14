package otel

import (
	"crypto/sha256"
	"fmt"
	"os"
	"sort"
	"strings"

	"ccwb-go/internal/jwt"
	"ccwb-go/internal/provider"
)

// UserInfo holds extracted user attributes from JWT claims.
type UserInfo struct {
	Email          string `json:"email"`
	UserID         string `json:"user_id"`
	Username       string `json:"username"`
	OrganizationID string `json:"organization_id"`
	Department     string `json:"department"`
	Team           string `json:"team"`
	CostCenter     string `json:"cost_center"`
	Manager        string `json:"manager"`
	Location       string `json:"location"`
	Role           string `json:"role"`
	Project        string `json:"project"`
	AccountUUID    string `json:"account_uuid"`
	Issuer         string `json:"issuer"`
	Subject        string `json:"subject"`
}

// ExtractUserInfo extracts user attributes from JWT claims with fallback chains.
// Kept as a default-key shim so callers that don't need the configurable
// cost-attribution key can stay unchanged; threads "Project" through to
// ExtractUserInfoWithTagKey.
func ExtractUserInfo(claims jwt.Claims) UserInfo {
	return ExtractUserInfoWithTagKey(claims, "Project")
}

// ExtractUserInfoWithTagKey is the same as ExtractUserInfo but reads the
// cost-attribution session tag under an arbitrary key name (e.g. "CostCenter").
// Callers that load config.json pass cfg.CostAttributionTagKey here, falling
// back to "Project" when that field is empty (older bundles predating the
// configurable key).
//
// Note: the AWS session-tag key is an IAM-level construct. The OTel header
// name (x-project) and collector dimension (project) remain unchanged and are
// independent of this key — they're our internal cost-attribution convention,
// not AWS state. A customer who sets CostAttributionTagKey="CostCenter" still
// sees the metric dimension labeled "project" in CloudWatch, with the value
// pulled from the CostCenter session tag.
func ExtractUserInfoWithTagKey(claims jwt.Claims, tagKey string) UserInfo {
	if tagKey == "" {
		tagKey = "Project"
	}
	info := UserInfo{}

	// Email
	info.Email = firstNonEmpty(
		claims.GetString("email"),
		claims.GetString("preferred_username"),
		claims.GetString("mail"),
	)
	if info.Email == "" {
		info.Email = "unknown@example.com"
	}

	// User ID - hash for privacy, format as UUID
	rawID := claims.GetString("sub")
	if rawID == "" {
		rawID = claims.GetString("user_id")
	}
	if rawID != "" {
		hash := sha256.Sum256([]byte(rawID))
		hex := fmt.Sprintf("%x", hash)
		// Take first 32 hex chars, format as 8-4-4-4-12
		h := hex[:32]
		info.UserID = fmt.Sprintf("%s-%s-%s-%s-%s", h[:8], h[8:12], h[12:16], h[16:20], h[20:32])
	}

	// Username
	info.Username = firstNonEmpty(
		claims.GetString("cognito:username"),
		claims.GetString("preferred_username"),
	)
	if info.Username == "" {
		info.Username = strings.SplitN(info.Email, "@", 2)[0]
	}

	// Organization - detect from issuer
	info.OrganizationID = "amazon-internal"
	if iss := claims.GetString("iss"); iss != "" {
		detected := provider.Detect(iss)
		if detected != "oidc" {
			info.OrganizationID = detected
		}
	}

	// Department
	info.Department = firstNonEmpty(
		claims.GetString("department"),
		claims.GetString("dept"),
		claims.GetString("division"),
	)
	if info.Department == "" {
		info.Department = "unspecified"
	}

	// Team (legacy default chain — deployments override via attribution_map)
	info.Team = firstNonEmpty(
		claims.GetString("team"),
		claims.GetString("team_id"),
		claims.GetString("group"),
	)
	if info.Team == "" {
		info.Team = "default-team"
	}

	// Cost center
	info.CostCenter = firstNonEmpty(
		claims.GetString("cost_center"),
		claims.GetString("costCenter"),
		claims.GetString("cost_code"),
	)
	if info.CostCenter == "" {
		info.CostCenter = "general"
	}

	// Manager
	info.Manager = firstNonEmpty(
		claims.GetString("manager"),
		claims.GetString("manager_email"),
	)
	if info.Manager == "" {
		info.Manager = "unassigned"
	}

	// Location
	info.Location = firstNonEmpty(
		claims.GetString("location"),
		claims.GetString("office_location"),
		claims.GetString("office"),
	)
	if info.Location == "" {
		info.Location = "remote"
	}

	// Role (legacy default chain — deployments override via attribution_map,
	// e.g. to carry the user's IdP group memberships for cost dashboards)
	info.Role = firstNonEmpty(
		claims.GetString("role"),
		claims.GetString("job_title"),
		claims.GetString("title"),
	)
	if info.Role == "" {
		info.Role = "user"
	}

	// Cost-attribution — from the AWS session-tag claim shipped by the IdP.
	// The claim key name comes from the profile config (default "Project",
	// override via cost_attribution_tag_key for customers using CostCenter /
	// BillingCode / etc). We still store the value in info.Project because
	// the downstream OTel header / dashboard dimension is "project" — that's
	// our internal convention, not AWS state. Intentionally left empty when
	// absent so FormatHeaders omits x-project; the static `project=default`
	// value baked into OTEL_RESOURCE_ATTRIBUTES at packaging time then carries
	// the dimension. (The collector does not map x-project from request
	// metadata, so omitting the header simply leaves that static default in
	// place rather than triggering any collector-side fallback.)
	info.Project = ExtractPrincipalTag(claims, tagKey)

	// Technical fields
	info.AccountUUID = claims.GetString("aud")
	info.Issuer = claims.GetString("iss")
	info.Subject = claims.GetString("sub")

	return info
}

// ExtractPrincipalTag returns the value of an AWS session-tag claim. STS and
// the IdP-side recipes in assets/docs/COST_ATTRIBUTION.md both accept two
// shapes:
//
//   - Flat:   {"https://aws.amazon.com/tags/principal_tags/<Key>": "<value>"}
//   - Nested: {"https://aws.amazon.com/tags": {"principal_tags": {"<Key>": "<value>" | ["<value>", ...]}}}
//
// Returns empty string when the tag isn't present or the claim is malformed.
// Caller should treat empty as "no value" rather than an error.
func ExtractPrincipalTag(claims jwt.Claims, tagKey string) string {
	// Flat form — most common on Okta (the claim name *is* the URL).
	if s, ok := claims["https://aws.amazon.com/tags/principal_tags/"+tagKey].(string); ok && s != "" {
		return s
	}

	// Nested form — common on Auth0 Actions / Azure claim transforms / Cognito
	// Pre-Token-Generation Lambdas. Value at principal_tags.<Key> may be either
	// a plain string or a single-element array (AWS STS accepts both).
	root, ok := claims["https://aws.amazon.com/tags"].(map[string]interface{})
	if !ok {
		return ""
	}
	principalTags, ok := root["principal_tags"].(map[string]interface{})
	if !ok {
		return ""
	}
	switch v := principalTags[tagKey].(type) {
	case string:
		return v
	case []interface{}:
		if len(v) > 0 {
			if s, ok := v[0].(string); ok {
				return s
			}
		}
	}
	return ""
}

// Attribution dimensions that a deployment may redefine via attribution_map
// in config.json. Every organization means something different by "role" or
// "team", so the map lets admins declare which sources feed each dimension
// instead of relying on the hardcoded legacy chains. Sources are tried in
// order; the first non-empty value wins; an empty resolution keeps the legacy
// default so dashboards never lose their bucket.
//
// Source expressions:
//
//	claim:<name>         string claim (or first entry when the claim is an array)
//	claims_sorted:<name> alpha-sorted "|"-joined string of an array claim (e.g. groups)
//	static:<key>         key from the OTEL_RESOURCE_ATTRIBUTES environment variable
//	literal:<value>      the value verbatim
//
// Mirrored in the Python otel_helper (_resolve_attribution_sources) — keep in sync.
var attributionTargets = map[string]func(*UserInfo) *string{
	"team.id":      func(i *UserInfo) *string { return &i.Team },
	"role":         func(i *UserInfo) *string { return &i.Role },
	"organization": func(i *UserInfo) *string { return &i.OrganizationID },
	"department":   func(i *UserInfo) *string { return &i.Department },
	"cost_center":  func(i *UserInfo) *string { return &i.CostCenter },
}

// ExtractUserInfoWithOptions applies the deployment's attribution_map on top
// of the legacy default chains. A nil/empty map is exactly
// ExtractUserInfoWithTagKey — existing deployments are untouched. statics is
// the deployment's static_resource_attributes from config.json (static:
// sources read it first, then the OTEL_RESOURCE_ATTRIBUTES environment
// variable — credential_process often runs outside Claude Code's env).
func ExtractUserInfoWithOptions(claims jwt.Claims, tagKey string, attribution map[string][]string, statics map[string]string) UserInfo {
	info := ExtractUserInfoWithTagKey(claims, tagKey)
	for dim, field := range attributionTargets {
		sources, ok := attribution[dim]
		if !ok {
			continue
		}
		if v := ResolveAttributionSources(claims, sources, statics); v != "" {
			*field(&info) = v
		}
	}
	return info
}

// ResolveAttributionSources evaluates ordered source expressions against the
// claims and static attributes, returning the first non-empty value.
func ResolveAttributionSources(claims jwt.Claims, sources []string, statics map[string]string) string {
	for _, source := range sources {
		kind, arg, ok := strings.Cut(source, ":")
		if !ok {
			continue
		}
		var v string
		switch kind {
		case "claim":
			v = firstNonEmpty(claims.GetString(arg), claims.GetFirstOfList(arg))
		case "claims_sorted":
			v = sortedJoinedList(claims, arg)
		case "static":
			v = firstNonEmpty(statics[arg], resourceAttrEnv(arg))
		case "literal":
			v = arg
		}
		if v != "" {
			return v
		}
	}
	return ""
}

// sortedJoinedList returns an array claim's string entries alpha-sorted and
// "|"-joined (e.g. groups ["b","a"] -> "a|b") so multi-group membership maps
// to one stable, bounded-cardinality dimension value. A plain string claim is
// returned verbatim.
func sortedJoinedList(claims jwt.Claims, key string) string {
	v, ok := claims[key]
	if !ok {
		return ""
	}
	switch val := v.(type) {
	case string:
		return val
	case []interface{}:
		var items []string
		for _, item := range val {
			if s, ok := item.(string); ok && s != "" {
				items = append(items, s)
			}
		}
		sort.Strings(items)
		return strings.Join(items, "|")
	}
	return ""
}

// resourceAttrEnv returns the value of a key from the OTEL_RESOURCE_ATTRIBUTES
// environment variable ("k=v,k=v" format), or "" when absent. The helpers run
// inside Claude Code's environment, so the deployment's static resource
// attributes are visible here and serve as fallbacks BELOW claim-derived
// values — resolving the precedence at header-generation time instead of
// relying on collector-side merge semantics.
func resourceAttrEnv(key string) string {
	raw := os.Getenv("OTEL_RESOURCE_ATTRIBUTES")
	if raw == "" {
		return ""
	}
	for _, pair := range strings.Split(raw, ",") {
		k, v, ok := strings.Cut(strings.TrimSpace(pair), "=")
		if ok && strings.TrimSpace(k) == key {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

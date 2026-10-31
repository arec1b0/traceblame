package normalizer

import (
	"context"

	"go.opentelemetry.io/collector/pdata/pcommon"
)

// Eval and fault-injection markers (#36). The gateway's tail sampling keeps every trace that
// carries one, so a client could force 100% sampling by setting it: only tenants in
// eval_tenants may (ADR-0005). Both tiers check, with the same list.
const (
	evalRunAttr        = "aegis.eval.run.id"
	faultInjectionAttr = "aegis.fault.injection.id"

	reasonTenantNotAllowed = "tenant_not_allowed"
	reasonScope            = "scope"
	reasonPattern          = "pattern"
	// noTenantLabel stands for a resource without a tenant (auth mode without auth).
	noTenantLabel = "_none"
)

// evalMarkers maps each marker to its profile scope.
var evalMarkers = map[string]string{evalRunAttr: "resource", faultInjectionAttr: "span"}

func isMarker(name string) bool {
	_, ok := evalMarkers[name]
	return ok
}

// keepMarker decides on a marker found at scope: the tenant must be allowlisted, the scope
// must be the marker's, and the value must pass the profile (type and pattern); v is
// repaired in place when it is kept. A marker in the wrong scope is still an attempt to
// mark the trace, so it is counted here, once, and not as an invalid attribute. Rejections
// are counted by tenant, attribute and reason, never with the value.
func (p *normalizerProcessor) keepMarker(ctx context.Context, scope, tenant, traceID, name string, v pcommon.Value) bool {
	label := tenant
	if label == "" {
		label = noTenantLabel
	}
	if !p.evalTenants[tenant] {
		p.tel.markerRejected.addLabels(ctx, traceID, label, name, reasonTenantNotAllowed)
		return false
	}
	if evalMarkers[name] != scope {
		p.tel.markerRejected.addLabels(ctx, traceID, label, name, reasonScope)
		return false
	}
	a, _ := p.profile.Attribute(name)
	coerced, ok := a.Coerce(v)
	if !ok {
		p.tel.markerRejected.addLabels(ctx, traceID, label, name, reasonPattern)
		return false
	}
	coerced.CopyTo(v)
	return true
}

// resourceTenant is the resource's tenant after setTenant ("" when there is none).
func resourceTenant(attrs pcommon.Map) string {
	if v, ok := attrs.Get(tenantAttr); ok && v.Type() == pcommon.ValueTypeStr {
		return v.Str()
	}
	return ""
}

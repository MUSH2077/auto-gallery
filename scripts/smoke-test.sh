#!/usr/bin/env bash
# auto-gallery E2E Smoke Test — API-level validation
# Usage: ./scripts/smoke-test.sh [--verbose]
#        SMOKE_PASS=yourpass ./scripts/smoke-test.sh
set -euo pipefail
cd "$(dirname "$0")/.."

RED='\033[0;31m' GREEN='\033[0;32m' YELLOW='\033[1;33m' NC='\033[0m'
PASS=0; FAIL=0; SKIP=0
pass() { echo -e "  ${GREEN}OK${NC} $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}FAIL${NC} $1 — $2"; FAIL=$((FAIL+1)); }
skip() { echo -e "  ${YELLOW}SKIP${NC} $1 ($2)"; SKIP=$((SKIP+1)); }

BASE="${SMOKE_BASE:-http://localhost:8818}"
ADMIN_PASS="${SMOKE_PASS:-}"
[ -z "$ADMIN_PASS" ] && [ -f .env ] && ADMIN_PASS=$(grep ADMIN_PASSWORD .env | cut -d= -f2)

echo "=== auto-gallery Smoke Test ==="
echo "  Target: $BASE"

# ── Auth ──
TOKEN=$(curl -sf -X POST "$BASE/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PASS\"}" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || true)

if [ -z "$TOKEN" ]; then
    fail "Login" "bad password — set SMOKE_PASS or check .env"
    echo "  Authenticated tests will be skipped. Example: SMOKE_PASS=xxx ./scripts/smoke-test.sh"
else
    pass "Login"
fi

H() { curl -sf -H "Authorization: Bearer $TOKEN" "$@"; }
AUTH_BLOCKED=0
auth_get() {
    local label="$1"
    local path="$2"
    local tmp code body
    tmp="$(mktemp)"
    code="$(curl -s -o "$tmp" -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$BASE$path" 2>/dev/null || true)"
    body="$(cat "$tmp" 2>/dev/null || true)"
    rm -f "$tmp"
    case "$code" in
        2*) pass "$label" ;;
        403)
            if echo "$body" | grep -q "Password change required"; then
                AUTH_BLOCKED=1
                fail "$label" "403 Password change required — rotate the bootstrap admin password, then rerun smoke"
            else
                fail "$label" "HTTP 403 — ${body:-forbidden}"
            fi
            ;;
        000) fail "$label" "request failed" ;;
        *) fail "$label" "HTTP $code — ${body:-no response body}" ;;
    esac
}

# ── Health ──
echo ""; echo "── Health ──"
curl -sf "$BASE/api/v1/system/ready" >/dev/null 2>&1 && pass "GET /system/ready" || fail "GET /system/ready" "unreachable"
curl -sf "$BASE/api/v1/system/health" >/dev/null 2>&1 && pass "GET /system/health" || fail "GET /system/health" "unreachable"

# ── Auth Guards ──
echo ""; echo "── Auth ──"
for ep in "/api/v1/search?q=test" "/api/v1/tags/00000000-0000-0000-0000-000000000001" "/api/v1/admin/operations"; do
    CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$ep" 2>/dev/null)
    [ "$CODE" = "401" ] && pass "auth required: $ep" || fail "auth: $ep" "got $CODE"
done

# ── Authenticated tests ──
if [ -z "$TOKEN" ]; then
    echo ""; echo "── Skipping authenticated tests (no token) ──"
    echo "Done. Run with: SMOKE_PASS=yourpass $0"
    exit 1
fi

# ── Workbench ──
echo ""; echo "── Workbench ──"
auth_get "GET /system/workbench" "/api/v1/system/workbench"

# ── Search (all 4 kinds) ──
echo ""; echo "── Search ──"
for k in all works creators tags; do
    auth_get "search kind=$k" "/api/v1/search?q=test&kind=$k"
done

if [ "$AUTH_BLOCKED" -eq 1 ]; then
    echo ""
    skip "remaining authenticated checks" "password change required"
    echo ""; echo "──────────────────────────────────"
    echo -e "Results: ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}, ${YELLOW}$SKIP skipped${NC}"
    echo -e "${RED}SMOKE TEST BLOCKED BY PASSWORD ROTATION${NC}"
    exit 1
fi

# ── Works detail ──
echo ""; echo "── Works ──"
WORK_ID=$(H "$BASE/api/v1/works?limit=1" 2>/dev/null | python3 -c "import sys,json; items=json.load(sys.stdin).get('items',[]); print(items[0]['id'] if items else '')" 2>/dev/null)
if [ -n "$WORK_ID" ]; then
    pass "list works"
    WD=$(H "$BASE/api/v1/works/$WORK_ID" 2>/dev/null)
    CID=$(echo "$WD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('creator_id') or '')" 2>/dev/null)
    [ -n "$CID" ] && pass "work detail has creator_id" || fail "work detail" "missing creator_id"
else
    skip "works" "no data in DB"
fi

# ── Tags detail ──
echo ""; echo "── Tags ──"
TAG_ID=$(H "$BASE/api/v1/tags?limit=1" 2>/dev/null | python3 -c "import sys,json; items=json.load(sys.stdin); print(items[0]['id'] if items else '')" 2>/dev/null)
if [ -n "$TAG_ID" ]; then
    pass "list tags"
    TD=$(H "$BASE/api/v1/tags/$TAG_ID" 2>/dev/null)
    echo "$TD" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'top_creators' in d" 2>/dev/null && pass "tag detail has top_creators" || fail "tag detail" "missing top_creators"
else
    skip "tags" "no data in DB"
fi

# ── Creators ──
echo ""; echo "── Creators ──"
auth_get "list creators" "/api/v1/creators?limit=1"

# ── Scheduler (no 500) ──
echo ""; echo "── Scheduler ──"
auth_get "scheduler-decisions" "/api/v1/system/scheduler-decisions"

# ── Operations ──
echo ""; echo "── Operations ──"
auth_get "admin/operations" "/api/v1/admin/operations"

# ── Summary ──
echo ""; echo "──────────────────────────────────"
echo -e "Results: ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}, ${YELLOW}$SKIP skipped${NC}"
if [ $FAIL -gt 0 ]; then
    echo -e "${RED}SMOKE TEST FAILED${NC}"
    exit 1
fi
echo -e "${GREEN}SMOKE TEST PASSED${NC}"

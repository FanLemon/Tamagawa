#!/usr/bin/env bash
# Smoke-test every API endpoint after deploying the service.
#
# Usage: ./after_install.sh [BASE_URL]        (default http://127.0.0.1:3007)
# Env:   YEAR_MONTH=YYYY-MM  month to read back (default: current month, Japan time)
#        SKIP_SYNC=1         don't run an OKCard import; read already-saved history
#
# Note: this calls the real BOCD site and (unless SKIP_SYNC=1) logs in to OKCard.
# Both writes are idempotent upserts. Exits 1 if any check fails.

set -u

BASE_URL="${1:-${BASE_URL:-http://127.0.0.1:3007}}"
YEAR_MONTH="${YEAR_MONTH:-$(TZ=Asia/Tokyo date +%Y-%m)}"
export YEAR_MONTH

BODY=$(mktemp)
trap 'rm -f "$BODY"' EXIT
passed=0
failed=0

# body_ok EXPR: evaluate a Python expression over the last response.
# `d` is the parsed JSON (None if not JSON), `t` the raw text, `ym` YEAR_MONTH.
body_ok() {
    python3 - "$BODY" "$1" <<'PY'
import json, os, sys
t = open(sys.argv[1], encoding="utf-8", errors="replace").read()
try:
    d = json.loads(t)
except ValueError:
    d = None
ym = os.environ["YEAR_MONTH"]
try:
    ok = eval(sys.argv[2])
except Exception:
    ok = False
sys.exit(0 if ok else 1)
PY
}

# check NAME METHOD PATH "ALLOWED STATUSES" [BODY_EXPR] [TIMEOUT_SECONDS]
check() {
    local name=$1 method=$2 path=$3 allowed=$4 expr=${5:-} timeout=${6:-60} status
    status=$(curl --silent --show-error --output "$BODY" --write-out '%{http_code}' \
        --max-time "$timeout" --request "$method" "$BASE_URL$path" 2>"$BODY.err") || status=000
    if [[ " $allowed " != *" $status "* ]]; then
        local detail
        detail=$(head -c 300 "$BODY"; cat "$BODY.err")
        printf 'FAIL  %-44s HTTP %s (expected %s) %s\n' "$name" "$status" "$allowed" "$detail"
        failed=$((failed + 1))
    elif [[ -n $expr ]] && ! body_ok "$expr"; then
        printf 'FAIL  %-44s HTTP %s, unexpected body: %s\n' "$name" "$status" "$(head -c 300 "$BODY")"
        failed=$((failed + 1))
    else
        printf 'ok    %-44s HTTP %s\n' "$name" "$status"
        passed=$((passed + 1))
    fi
    rm -f "$BODY.err"
}

echo "Testing $BASE_URL (month $YEAR_MONTH)"
echo

echo "# BOCD exchange rates"
check "POST /fetch_bocd_exchange_rates" POST /fetch_bocd_exchange_rates "200" \
    'isinstance(d, list) and d and all(k in d[0] for k in ("currency", "sell", "published_at"))' 120
check "GET  /bocd_exchange_rates" GET /bocd_exchange_rates "200" \
    'isinstance(d, list) and 0 < len(d) <= 100'
check "GET  /bocd_exchange_rates?limit=1&offset=1" GET "/bocd_exchange_rates?limit=1&offset=1" "200" \
    'isinstance(d, list) and len(d) == 1'
check "GET  /bocd_exchange_rates_count" GET /bocd_exchange_rates_count "200" \
    'isinstance(d, int) and d > 0'
check "GET  /latest_jpy_sell" GET /latest_jpy_sell "200" \
    'd["currency"] == "日元" and float(d["sell"]) > 0 and d["published_at"]'
check "rejects limit=-1" GET "/bocd_exchange_rates?limit=-1" "422"
check "rejects limit=101" GET "/bocd_exchange_rates?limit=101" "422"
check "rejects offset=-1" GET "/bocd_exchange_rates?offset=-1" "422"
echo

echo "# OKCard purchase history"
if [[ ${SKIP_SYNC:-} == 1 ]]; then
    echo "skip  POST /okcard_history/sync                   (SKIP_SYNC=1)"
else
    # 409 means the nightly timer's sync is running right now; not a deploy failure.
    check "POST /okcard_history/sync" POST /okcard_history/sync "200 409" \
        'all(k in d for k in ("fetched", "inserted", "updated", "unchanged")) or "detail" in d' 310
fi
check "GET  /okcard_history/sync_status" GET /okcard_history/sync_status "200" \
    'd["has_synced"] is True and d["last_success_at"]'
check "GET  /okcard_history (json)" GET "/okcard_history?year_month=$YEAR_MONTH&format=json" "200" \
    'd["year_month"] == ym and isinstance(d["purchases"], list) and set(d["summary"]) == {"purchases", "total_items", "total_spent"}'
check "GET  /okcard_history (html)" GET "/okcard_history?year_month=$YEAR_MONTH&format=html" "200" \
    '"<script id=\"data\"" in t and ("\"year_month\":\"" + ym + "\"") in t'
check "GET  /okcard_history (default html)" GET "/okcard_history?year_month=$YEAR_MONTH" "200" \
    '"<script id=\"data\"" in t'
check "rejects year_month=bad" GET "/okcard_history?year_month=bad" "422"
check "rejects year_month=2026-13" GET "/okcard_history?year_month=2026-13" "422"
check "rejects format=xml" GET "/okcard_history?year_month=$YEAR_MONTH&format=xml" "422"
echo

echo "$passed passed, $failed failed"
[[ $failed -eq 0 ]]

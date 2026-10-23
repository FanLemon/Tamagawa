"""Linux CLI: inspect purchase fields or import history using the service config."""

import argparse
import json
import sys

from fastapi import HTTPException

import main
from okcard_storage import purchase_identity


def inspect_identities(purchases):
    """Diagnose repeated IDs without printing the IDs or receipt values."""
    checks = []
    for fields in [("id",), ("id", "store_name"), ("id", "store_name", "purchase_date")]:
        keys = []
        for row in purchases:
            try:
                keys.append(purchase_identity(row, fields))
            except ValueError:
                pass
        checks.append({
            "fields": list(fields), "valid_purchases": len(keys),
            "distinct_keys": len(set(keys)),
            "unique_in_every_purchase": bool(purchases) and len(keys) == len(purchases) == len(set(keys)),
        })
    groups = {}
    for row in purchases:
        try:
            key = purchase_identity(row, ("id",))
        except ValueError:
            continue
        groups.setdefault(key, []).append(row)
    repeated = []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        fields = sorted({name for row in rows for name in row})
        repeated.append({
            "purchases": len(rows),
            "differing_fields": [name for name in fields if len({
                json.dumps([name in row, row.get(name)], sort_keys=True) for row in rows
            }) > 1],
            "exact_duplicate_rows": len(rows) - len({json.dumps(row, sort_keys=True) for row in rows}),
        })
    return {"identity_checks": checks, "repeated_id_groups": repeated}


def inspect_fields(purchases):
    """Report field coverage/uniqueness, never receipt values or credentials."""
    if not isinstance(purchases, list) or any(not isinstance(row, dict) for row in purchases):
        raise ValueError("History must be a list of purchase objects")
    fields = sorted({name for row in purchases for name in row})
    result = []
    for name in fields:
        values = [row[name] for row in purchases if name in row]
        usable = [str(value) for value in values if
                  not isinstance(value, bool) and isinstance(value, (str, int)) and str(value).strip()]
        result.append({
            "field": name,
            "present": len(values),
            "types": sorted({type(value).__name__ for value in values}),
            "distinct_id_values": len(set(usable)),
            "unique_nonempty_scalar_in_every_purchase":
                bool(purchases) and len(usable) == len(purchases) == len(set(usable)),
        })
    diagnostics = inspect_identities(purchases) if "id" in fields else {}
    return {"purchases": len(purchases), "fields": result, **diagnostics,
            "note": "Sample uniqueness does not prove stability. Composite identity components must remain unchanged when receipts are corrected."}


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true", help="List response field names and counts; do not write to DB")
    args = parser.parse_args()
    try:
        if args.inspect:
            result = inspect_fields(main.okcard_cached_history(*main.okcard_credentials()))
        else:
            result = main.sync_okcard_history()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except HTTPException as exc:
        print(f"Error ({exc.status_code}): {exc.detail}", file=sys.stderr)
    except Exception as exc:
        # Driver exceptions may include SQL parameters with receipt contents.
        print(f"Command failed ({type(exc).__name__})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(run())

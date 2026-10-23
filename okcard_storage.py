"""PostgreSQL persistence for OKCard purchases; no network calls on reads."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Optional

from sqlalchemy import Column, DateTime, Index, Numeric, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlmodel import Field, Session, SQLModel, select


JST = timezone(timedelta(hours=9))
# Rows per INSERT ... ON CONFLICT; 8 columns each stays well under Postgres's 65535 bind-parameter limit.
UPSERT_BATCH_SIZE = 500


class OKCardPurchase(SQLModel, table=True):
    __tablename__ = "okcard_purchases"
    __table_args__ = (
        UniqueConstraint("account_key", "source_purchase_id", name="uq_okcard_purchase_source"),
        Index("ix_okcard_purchase_month", "account_key", "purchase_date"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_key: str
    source_purchase_id: str
    purchase_date: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    store_name: str
    purchase_price: Decimal = Field(sa_column=Column(Numeric(18, 4), nullable=False))
    raw_payload: dict = Field(sa_column=Column(JSONB, nullable=False))
    first_seen_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class OKCardSyncState(SQLModel, table=True):
    __tablename__ = "okcard_sync_state"

    account_key: str = Field(primary_key=True)
    identity_field: str
    last_attempt_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    last_success_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    last_error: Optional[str] = None


class SyncInProgress(Exception):
    pass


class HistoryNotSynced(Exception):
    pass


class SyncConfigurationError(ValueError):
    pass


def account_key(login_id: str) -> str:
    return hashlib.sha256(login_id.encode("utf-8")).hexdigest()


def identity_fields(specification: str) -> tuple:
    """Retain single-field compatibility; accept an ordered composite key."""
    fields = tuple(field.strip() for field in specification.split(","))
    if not all(fields) or len(set(fields)) != len(fields):
        raise SyncConfigurationError("Purchase ID fields must be nonempty, distinct field names")
    return fields


def purchase_identity(purchase: dict, fields: tuple) -> str:
    parts = []
    for field in fields:
        value = purchase.get(field)
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
            raise ValueError("Missing or invalid configured purchase ID component")
        parts.append(str(value))
    # JSON arrays avoid ambiguous delimiter concatenation. Preserve old keys
    # exactly for single-field configurations already stored in the database.
    return parts[0] if len(parts) == 1 else json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def month_bounds(year_month: str):
    try:
        start = datetime.strptime(year_month, "%Y-%m").replace(tzinfo=JST)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    except (ValueError, OverflowError) as exc:
        raise ValueError("year_month must be a valid YYYY-MM month") from exc
    return start, end


def _number(value, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid {label}") from exc
    if not result.is_finite():
        raise ValueError(f"Invalid {label}")
    return result


def normalize_purchases(purchases: list, identity_field: str) -> list:
    """Validate the entire batch before opening its write transaction."""
    if not isinstance(purchases, list):
        raise ValueError("History response must be a list")
    fields = identity_fields(identity_field)
    rows = []
    seen = set()
    for position, purchase in enumerate(purchases):
        try:
            if not isinstance(purchase, dict):
                raise ValueError("Purchase must be an object")
            source_id = purchase_identity(purchase, fields)
            if source_id in seen:
                raise ValueError("Duplicate purchase ID in response; verify the configured fields")
            seen.add(source_id)
            date_value = purchase.get("purchase_date")
            if not isinstance(date_value, str):
                raise ValueError("Missing purchase_date")
            purchased_at = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
            if purchased_at.tzinfo is None:
                purchased_at = purchased_at.replace(tzinfo=JST)
            store = purchase.get("store_name")
            if not isinstance(store, str) or not store.strip():
                raise ValueError("Missing store_name")
            price = _number(purchase.get("purchase_price"), "purchase_price")
            if abs(price) >= Decimal("100000000000000") or price != price.quantize(Decimal("0.0001")):
                raise ValueError("purchase_price exceeds NUMERIC(18,4) precision")
            items = purchase.get("items")
            if not isinstance(items, list):
                raise ValueError("items must be a list")
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("item_name"), str):
                    raise ValueError("Invalid item")
                _number(item.get("item_count"), "item_count")
                _number(item.get("price"), "item price")
            # Reject non-JSON values and NaN rather than losing data in storage.
            raw = json.loads(json.dumps(purchase, allow_nan=False))
            rows.append(dict(source_purchase_id=source_id, purchase_date=purchased_at,
                             store_name=store, purchase_price=price, raw_payload=raw))
        except (ValueError, TypeError, InvalidOperation) as exc:
            # Do not include receipt contents in error messages.
            raise ValueError(f"Invalid purchase at index {position}: {exc}") from exc
    return rows


@contextmanager
def _sync_connection(engine, key: str):
    """Hold a session advisory lock across fetch and commit, across workers."""
    lock_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
    with engine.connect() as connection:
        locked = connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_id}).scalar_one()
        connection.commit()
        if not locked:
            raise SyncInProgress("A sync for this account is already running")
        try:
            yield connection
        finally:
            try:
                connection.rollback()
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id})
                connection.commit()
            except Exception:
                # Never put a connection with a possibly held lock back in the pool.
                connection.invalidate()
                raise


def sync_history(engine, key: str, identity_field: str, fetch_history) -> dict:
    if not identity_field:
        raise ValueError("Set OKCARD_PURCHASE_ID_FIELD after inspecting the response")
    identity_field = ",".join(identity_fields(identity_field))
    with _sync_connection(engine, key) as connection:
        attempted_at = datetime.now(timezone.utc)
        with Session(connection) as session, session.begin():
            state = session.get(OKCardSyncState, key)
            if state and state.last_success_at is not None and state.identity_field != identity_field:
                raise SyncConfigurationError("Purchase ID field changed; migrate existing identities before syncing")
            if state is None:
                state = OKCardSyncState(account_key=key, identity_field=identity_field)
            state.identity_field = identity_field
            state.last_attempt_at = attempted_at
            session.add(state)

        try:
            rows = normalize_purchases(fetch_history(), identity_field)
            counts = {"fetched": len(rows), "inserted": 0, "updated": 0, "unchanged": 0}
            synced_at = datetime.now(timezone.utc)
            with Session(connection) as session, session.begin():
                # Only the two compared columns, not full ORM rows.
                existing = dict(session.exec(
                    select(OKCardPurchase.source_purchase_id, OKCardPurchase.raw_payload)
                    .where(OKCardPurchase.account_key == key)).all())
                changed = []
                for row in rows:
                    source_id = row["source_purchase_id"]
                    if source_id in existing and existing[source_id] == row["raw_payload"]:
                        counts["unchanged"] += 1
                        continue
                    counts["updated" if source_id in existing else "inserted"] += 1
                    changed.append({**row, "account_key": key, "first_seen_at": synced_at, "updated_at": synced_at})
                # Multi-row upserts; IDs are unique within a batch (normalize_purchases).
                for start in range(0, len(changed), UPSERT_BATCH_SIZE):
                    statement = insert(OKCardPurchase).values(changed[start:start + UPSERT_BATCH_SIZE])
                    session.execute(statement.on_conflict_do_update(
                        constraint="uq_okcard_purchase_source",
                        set_={name: getattr(statement.excluded, name) for name in (
                            "purchase_date", "store_name", "purchase_price", "raw_payload", "updated_at")},
                    ))
                state = session.get(OKCardSyncState, key)
                state.last_success_at = synced_at
                state.last_error = None
                session.add(state)
            return {**counts, "last_success_at": synced_at.isoformat()}
        except Exception as exc:
            # Transaction above has rolled back. Keep the previous success marker.
            with Session(connection) as session, session.begin():
                state = session.get(OKCardSyncState, key)
                state.last_error = f"Sync failed ({type(exc).__name__})"
                session.add(state)
            raise


def read_month(session: Session, key: str, year_month: str) -> list:
    start, end = month_bounds(year_month)
    state = session.get(OKCardSyncState, key)
    if state is None or state.last_success_at is None:
        raise HistoryNotSynced("No successful import yet; run POST /okcard_history/sync first")
    rows = session.exec(select(OKCardPurchase).where(
        OKCardPurchase.account_key == key,
        OKCardPurchase.purchase_date >= start,
        OKCardPurchase.purchase_date < end,
    ).order_by(OKCardPurchase.purchase_date, OKCardPurchase.id)).all()
    return [row.raw_payload for row in rows]


def sync_status(session: Session, key: str) -> dict:
    state = session.get(OKCardSyncState, key)
    return {
        "has_synced": state is not None and state.last_success_at is not None,
        "identity_field": state.identity_field if state else None,
        "last_attempt_at": state.last_attempt_at if state else None,
        "last_success_at": state.last_success_at if state else None,
        "last_error": state.last_error if state else None,
    }

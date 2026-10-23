import concurrent.futures
from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import urllib.error
import uuid

from sqlalchemy import event, text
from sqlmodel import Session, create_engine, select

# main requires DATABASE_URL at import; these tests never connect to it.
os.environ.setdefault("DATABASE_URL", "postgresql://unused@localhost/unused")
import main
import okcard_storage as storage
from okcard_sync import inspect_fields


PURCHASE = {
    "receipt_id": "receipt-1", "purchase_date": "2026-09-30 23:59:59",
    "store_name": "Test store", "purchase_price": "100",
    "items": [{"item_name": "Test item", "item_count": "2", "price": "50"}],
    "extra_field": {"preserved": True},
}


class StorageValidationTests(unittest.TestCase):
    def test_raw_payload_and_decimal_are_preserved(self):
        row = storage.normalize_purchases([PURCHASE], "receipt_id")[0]
        self.assertEqual(row["raw_payload"], PURCHASE)
        self.assertEqual(row["purchase_price"], Decimal("100"))
        self.assertEqual(row["purchase_date"].utcoffset().total_seconds(), 9 * 3600)

    def test_bad_rows_reject_entire_batch(self):
        for changes in [
            {"receipt_id": None}, {"receipt_id": True}, {"purchase_date": "bad"},
            {"store_name": None}, {"purchase_price": "NaN"},
            {"purchase_price": "100000000000000"}, {"purchase_price": "0.00001"},
            {"items": {}}, {"items": [{"item_name": "Bad", "item_count": "NaN", "price": "1"}]},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                storage.normalize_purchases([PURCHASE, {**PURCHASE, "receipt_id": "other", **changes}], "receipt_id")

    def test_duplicate_ids_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate purchase ID"):
            storage.normalize_purchases([PURCHASE, PURCHASE], "receipt_id")

    def test_composite_key_preserves_receipts_with_repeated_ids(self):
        purchases = [
            {**PURCHASE, "id": "shared"},
            {**PURCHASE, "id": "shared", "purchase_date": "2026-09-29 12:00:00"},
            {**PURCHASE, "id": "shared", "store_name": "Another store"},
        ]
        rows = storage.normalize_purchases(purchases, "id, store_name, purchase_date")
        self.assertEqual(len({row["source_purchase_id"] for row in rows}), 3)
        self.assertEqual([row["raw_payload"] for row in rows], purchases)

    def test_composite_identity_survives_price_and_item_corrections(self):
        specification = "receipt_id,store_name,purchase_date"
        original = storage.normalize_purchases([PURCHASE], specification)[0]
        corrected = storage.normalize_purchases([
            {**PURCHASE, "purchase_price": "200", "items": []}
        ], specification)[0]
        self.assertEqual(original["source_purchase_id"], corrected["source_purchase_id"])
        self.assertNotEqual(original["raw_payload"], corrected["raw_payload"])

    def test_composite_duplicates_and_missing_components_are_rejected(self):
        specification = "receipt_id,store_name,purchase_date"
        with self.assertRaisesRegex(ValueError, "Duplicate purchase ID"):
            storage.normalize_purchases([PURCHASE, {**PURCHASE, "purchase_price": "200"}], specification)
        for value in [None, "", "   ", True, []]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                storage.normalize_purchases([{**PURCHASE, "receipt_id": value}], specification)

    def test_key_encoding_is_unambiguous_and_single_keys_are_compatible(self):
        first = storage.purchase_identity({"a": "x,y", "b": "z"}, ("a", "b"))
        second = storage.purchase_identity({"a": "x", "b": "y,z"}, ("a", "b"))
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads(first), ["x,y", "z"])
        self.assertEqual(storage.purchase_identity(PURCHASE, ("receipt_id",)), "receipt-1")

    def test_identity_configuration_rejects_empty_or_repeated_fields(self):
        for specification in ["", ",", "id,", "id, id", "id,,store_name"]:
            with self.subTest(specification=specification), self.assertRaises(storage.SyncConfigurationError):
                storage.identity_fields(specification)
        self.assertEqual(storage.identity_fields("id, store_name"), ("id", "store_name"))

    def test_month_bounds_include_december_rollover(self):
        start, end = storage.month_bounds("2026-12")
        self.assertEqual(start.isoformat(), "2026-12-01T00:00:00+09:00")
        self.assertEqual(end.isoformat(), "2027-01-01T00:00:00+09:00")
        for value in ["2026-00", "2026-13", "0000-01", "bad"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                storage.month_bounds(value)

    def test_inspection_exposes_only_names_and_statistics(self):
        result = inspect_fields([PURCHASE])
        report = json.dumps(result)
        self.assertNotIn("receipt-1", report)
        self.assertNotIn("Test store", report)
        fields = {field["field"]: field for field in result["fields"]}
        self.assertTrue(fields["receipt_id"]["unique_nonempty_scalar_in_every_purchase"])
        self.assertFalse(fields["items"]["unique_nonempty_scalar_in_every_purchase"])
        self.assertEqual(inspect_fields([])["purchases"], 0)

    def test_inspection_explains_id_collisions_without_receipt_values(self):
        purchases = [
            {**PURCHASE, "id": "private-id"},
            {**PURCHASE, "id": "private-id", "purchase_date": "2026-09-29 12:00:00"},
        ]
        report = inspect_fields(purchases)
        self.assertEqual(report["repeated_id_groups"], [
            {"purchases": 2, "differing_fields": ["purchase_date"], "exact_duplicate_rows": 0}
        ])
        self.assertFalse(report["identity_checks"][0]["unique_in_every_purchase"])
        self.assertFalse(report["identity_checks"][1]["unique_in_every_purchase"])
        self.assertTrue(report["identity_checks"][2]["unique_in_every_purchase"])
        output = json.dumps(report)
        for private in ("private-id", "Test store", "2026-09-29", "receipt-1"):
            self.assertNotIn(private, output)
        repeated = inspect_fields([purchases[0], purchases[0]])
        self.assertEqual(repeated["repeated_id_groups"][0]["exact_duplicate_rows"], 1)
        self.assertFalse(repeated["identity_checks"][2]["unique_in_every_purchase"])

    def test_history_route_reads_database_without_upstream(self):
        with patch.dict(os.environ, {"OKCARD_USER": "test-user"}), \
                patch.object(storage, "read_month", return_value=[PURCHASE]) as read, \
                patch.object(main, "okcard_cached_history") as upstream:
            result = main.read_okcard_history(session=None, year_month="2026-09", fmt="json")
            html = main.read_okcard_history(session=None, year_month="2026-09", fmt="html")
        upstream.assert_not_called()
        self.assertEqual(result["purchases"], [PURCHASE])
        self.assertEqual(result["summary"], {"purchases": 1, "total_items": 2, "total_spent": 100})
        self.assertIn(b'Test store', html.body)
        read.assert_called_with(None, storage.account_key("test-user"), "2026-09")

    def test_never_synced_route_returns_503(self):
        with patch.dict(os.environ, {"OKCARD_USER": "test-user"}), \
                patch.object(storage, "read_month", side_effect=storage.HistoryNotSynced("Sync first")):
            with self.assertRaises(main.HTTPException) as exc:
                main.read_okcard_history(session=None, year_month="2026-09", fmt="json")
        self.assertEqual(exc.exception.status_code, 503)

    def test_sync_requires_explicit_identity_configuration(self):
        with patch.dict(os.environ, {"OKCARD_USER": "test", "OKCARD_PASS": "pass",
                                     "OKCARD_PURCHASE_ID_FIELD": ""}), \
                patch.object(storage, "sync_history") as sync:
            with self.assertRaises(main.HTTPException) as exc:
                main.sync_okcard_history()
        self.assertEqual(exc.exception.status_code, 500)
        sync.assert_not_called()

    def test_sync_route_reports_conflict_and_upstream_failure(self):
        for error, status in [(storage.SyncInProgress("busy"), 409),
                              (storage.SyncConfigurationError("changed"), 500),
                              (ValueError("Invalid purchase"), 502),
                              (TimeoutError("timeout"), 502),
                              (urllib.error.URLError(ConnectionResetError()), 502)]:
            with self.subTest(error=error), \
                    patch.dict(os.environ, {"OKCARD_USER": "test", "OKCARD_PASS": "pass",
                                           "OKCARD_PURCHASE_ID_FIELD": "receipt_id"}), \
                    patch.object(storage, "sync_history", side_effect=error):
                with self.assertRaises(main.HTTPException) as exc:
                    main.sync_okcard_history_endpoint()
                self.assertEqual(exc.exception.status_code, status)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL to run PostgreSQL integration tests")
class PostgresStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "okcard_test_" + uuid.uuid4().hex
        cls.admin = create_engine(os.environ["TEST_DATABASE_URL"])
        cls.addClassCleanup(cls.admin.dispose)
        with cls.admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.addClassCleanup(cls.drop_test_schema)
        cls.engine = create_engine(os.environ["TEST_DATABASE_URL"],
                                   connect_args={"options": f"-csearch_path={cls.schema}"})
        cls.addClassCleanup(cls.engine.dispose)
        # Exercise the shipped migration, including repeat application.
        migration = (Path(__file__).parent / "migrations/001_okcard_history.sql").read_text()
        with cls.engine.connect() as conn:
            for _ in range(2):
                conn.exec_driver_sql(migration)
                conn.commit()

    @classmethod
    def drop_test_schema(cls):
        with cls.admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))

    def setUp(self):
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE okcard_purchases, okcard_sync_state RESTART IDENTITY"))
        self.key = storage.account_key("test-user")

    def sync(self, purchases, key=None):
        return storage.sync_history(self.engine, key or self.key, "receipt_id", lambda: deepcopy(purchases))

    def read(self, month="2026-09", key=None):
        with Session(self.engine) as session:
            return storage.read_month(session, key or self.key, month)

    def status(self):
        with Session(self.engine) as session:
            return storage.sync_status(session, self.key)

    def test_repeat_import_and_corrections(self):
        self.assertEqual(self.sync([PURCHASE])["inserted"], 1)
        first_success = self.status()["last_success_at"]
        self.assertEqual(self.sync([PURCHASE])["unchanged"], 1)
        corrected = {**PURCHASE, "purchase_price": "120"}
        self.assertEqual(self.sync([corrected])["updated"], 1)
        self.assertEqual(self.read(), [corrected])
        with Session(self.engine) as session:
            row = session.exec(select(storage.OKCardPurchase)).one()
            self.assertEqual(row.first_seen_at, first_success)
            self.assertEqual(row.purchase_price, Decimal("120"))

    def test_import_spanning_several_upsert_batches(self):
        count = storage.UPSERT_BATCH_SIZE * 2 + 1
        purchases = [{**PURCHASE, "receipt_id": f"r{i}"} for i in range(count)]
        self.assertEqual(self.sync(purchases)["inserted"], count)
        purchases[0]["purchase_price"] = "120"
        purchases[-1]["purchase_price"] = "130"
        purchases.append({**PURCHASE, "receipt_id": "new"})
        result = self.sync(purchases)
        self.assertEqual((result["inserted"], result["updated"], result["unchanged"]), (1, 2, count - 2))
        self.assertEqual(len(self.read()), count + 1)
        with Session(self.engine) as session:
            prices = dict(session.exec(select(storage.OKCardPurchase.source_purchase_id,
                                              storage.OKCardPurchase.purchase_price)).all())
        self.assertEqual((prices["r0"], prices[f"r{count - 1}"], prices["r1"]),
                         (Decimal("120"), Decimal("130"), Decimal("100")))

    def test_composite_import_repeat_and_correction(self):
        purchases = [{**PURCHASE, "id": "shared"},
                     {**PURCHASE, "id": "shared", "purchase_date": "2026-09-29 12:00:00"}]
        specification = "id,store_name,purchase_date"
        def sync():
            return storage.sync_history(self.engine, self.key, specification, lambda: deepcopy(purchases))
        self.assertEqual(sync()["inserted"], 2)
        self.assertEqual(sync()["unchanged"], 2)
        purchases[0]["purchase_price"] = "200"
        result = sync()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(self.read(), [purchases[1], purchases[0]])
        self.assertEqual(self.status()["identity_field"], specification)

    def test_account_isolation_and_japan_month_boundaries(self):
        rows = [
            {**PURCHASE, "receipt_id": "start", "purchase_date": "2026-08-31T15:00:00Z"},
            PURCHASE,
            {**PURCHASE, "receipt_id": "next", "purchase_date": "2026-09-30T15:00:00Z"},
        ]
        self.sync(rows)
        other = storage.account_key("other-user")
        self.sync([PURCHASE], key=other)
        self.assertEqual([p["receipt_id"] for p in self.read()], ["start", "receipt-1"])
        self.assertEqual([p["receipt_id"] for p in self.read("2026-10")], ["next"])
        self.assertEqual(self.read(key=other), [PURCHASE])

    def test_empty_and_never_synced_are_different(self):
        with self.assertRaises(storage.HistoryNotSynced):
            self.read()
        self.sync([])
        self.assertEqual(self.read(), [])
        self.assertTrue(self.status()["has_synced"])

    def test_absent_purchases_are_retained(self):
        self.sync([PURCHASE])
        self.sync([])
        self.assertEqual(self.read(), [PURCHASE])

    def test_validation_and_upstream_failures_preserve_previous_import(self):
        self.sync([PURCHASE])
        previous_success = self.status()["last_success_at"]
        with self.assertRaises(ValueError):
            self.sync([{**PURCHASE, "purchase_price": "200"}, {"receipt_id": "invalid"}])
        self.assertEqual(self.read(), [PURCHASE])
        def failed_fetch():
            raise TimeoutError("upstream timeout")
        with self.assertRaises(TimeoutError):
            storage.sync_history(self.engine, self.key, "receipt_id", failed_fetch)
        self.assertEqual(self.status()["last_success_at"], previous_success)
        self.assertIn("TimeoutError", self.status()["last_error"])
        self.assertEqual(self.read(), [PURCHASE])
        self.sync([PURCHASE])
        self.assertIsNone(self.status()["last_error"])

    def test_write_failure_rolls_back_entire_import(self):
        self.sync([PURCHASE])
        previous_success = self.status()["last_success_at"]
        writes = []
        def fail_second_insert(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO okcard_purchases"):
                writes.append(statement)
                if len(writes) == 2:
                    raise RuntimeError("Simulated write failure")
        event.listen(self.engine, "before_cursor_execute", fail_second_insert)
        try:
            with self.assertRaises(RuntimeError):
                self.sync([{**PURCHASE, "purchase_price": "200"}, {**PURCHASE, "receipt_id": "new"}])
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_second_insert)
        self.assertEqual(len(writes), 2)
        self.assertEqual(self.read(), [PURCHASE])
        self.assertEqual(self.status()["last_success_at"], previous_success)

    def test_overlapping_sync_is_rejected_and_lock_is_released(self):
        fetching, release = threading.Event(), threading.Event()
        def fetch():
            fetching.set()
            if not release.wait(5):
                raise TimeoutError("Test timed out")
            return [PURCHASE]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(storage.sync_history, self.engine, self.key, "receipt_id", fetch)
            try:
                self.assertTrue(fetching.wait(5))
                with self.assertRaises(storage.SyncInProgress):
                    self.sync([PURCHASE])
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5)["inserted"], 1)
        self.assertEqual(self.sync([PURCHASE])["unchanged"], 1)

    def test_identity_field_change_is_rejected(self):
        self.sync([PURCHASE])
        with self.assertRaises(storage.SyncConfigurationError):
            storage.sync_history(self.engine, self.key, "other_id", lambda: [])
        self.assertEqual(self.read(), [PURCHASE])


if __name__ == "__main__":
    unittest.main()

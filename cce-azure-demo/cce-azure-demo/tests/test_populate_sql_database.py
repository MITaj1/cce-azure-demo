import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sql_database"))

import ukbank_data as ub  # noqa: E402
import populate_sql_database as psd  # noqa: E402


# ---------------------------------------------------------------------------
# Fake pyodbc connection/cursor - an in-memory table store standing in for
# Azure SQL, since no real SQL Server endpoint is reachable in this sandbox.
# Tracks peak in-flight batch size so we can assert memory-safety directly.
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._last_result = None

    def execute(self, sql, *params):
        sql_stripped = sql.strip().upper()
        if sql_stripped.startswith("SELECT SUM(ROW_COUNT) FROM SYS.DM_DB_PARTITION_STATS"):
            # Simulate the catalog view existing but returning NULL (as it
            # would on a brand-new/empty table) to force the COUNT(*) fallback
            # path to be exercised too.
            self._last_result = (None,)
        elif sql_stripped.startswith("SELECT COUNT(*) FROM"):
            table = sql.split("FROM")[1].strip()
            self._last_result = (len(self.conn.tables.get(table, [])),)
        elif sql_stripped.startswith("SELECT ISNULL(MAX("):
            table = sql.split("FROM")[1].strip()
            rows = self.conn.tables.get(table, [])
            self._last_result = (len(rows),)  # our fake IDs are just 1..N by insertion order
        elif sql_stripped.startswith("IF NOT EXISTS"):
            # Schema statement - just record that it ran.
            self.conn.schema_statements_executed.append(sql.strip())
        else:
            raise AssertionError(f"FakeCursor got unexpected SQL: {sql!r}")

    def executemany(self, sql, rows):
        table = sql.split("INSERT INTO")[1].split("(")[0].strip()
        self.conn.tables.setdefault(table, [])
        self.conn.tables[table].extend(rows)
        self.conn.peak_batch_size = max(self.conn.peak_batch_size, len(rows))
        self.conn.batch_sizes_seen.setdefault(table, []).append(len(rows))

    def fetchone(self):
        return self._last_result


class FakeConnection:
    def __init__(self):
        self.tables = {}
        self.schema_statements_executed = []
        self.peak_batch_size = 0
        self.batch_sizes_seen = {}
        self.commit_count = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commit_count += 1


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_ensure_schema_executes_exactly_three_table_guards():
    conn = FakeConnection()
    psd.ensure_schema(conn)
    assert len(conn.schema_statements_executed) == 3
    assert conn.schema_statements_executed[0].startswith("IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'customers'")
    assert conn.schema_statements_executed[1].startswith("IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'accounts'")
    assert conn.schema_statements_executed[2].startswith("IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'transactions'")


# ---------------------------------------------------------------------------
# Row-size / batch-size sanity (the OOM-avoidance piece, exercised end to end)
# ---------------------------------------------------------------------------

def test_configured_row_sizes_produce_sane_batch_sizes_at_default_budget():
    # At the generous 256MB default, our banking-row sizes are small enough
    # that the max_batch ceiling (50,000) is expected to bind for all three
    # tables - that's fine, 50,000 rows is a normal bulk-insert size. What
    # actually matters (tested next) is that a *tight* budget genuinely
    # shrinks the batch below that ceiling - that's the OOM-protective path.
    default_budget = 256 * 1024 * 1024
    for row_size, label in [
        (psd.CUSTOMER_ROW_SIZE, "customers"),
        (psd.ACCOUNT_ROW_SIZE, "accounts"),
        (psd.TRANSACTION_ROW_SIZE, "transactions"),
    ]:
        batch = ub.calculate_batch_size(row_size, default_budget)
        assert 50 < batch <= 50_000, f"{label}: batch size {batch} out of sane range for row_size={row_size}"


def test_transactions_batch_shrinks_with_smaller_memory_budget():
    big_budget = ub.calculate_batch_size(psd.TRANSACTION_ROW_SIZE, 256 * 1024 * 1024)
    tiny_budget = ub.calculate_batch_size(psd.TRANSACTION_ROW_SIZE, 2 * 1024 * 1024)
    assert tiny_budget < big_budget
    # And the tight-budget batch should itself be well below the ceiling,
    # proving it's the memory term doing the work here, not the cap.
    assert tiny_budget < 50_000


# ---------------------------------------------------------------------------
# Population + resumability
# ---------------------------------------------------------------------------

def test_populate_customers_fresh_reaches_target_in_bounded_batches():
    conn = FakeConnection()
    psd.populate_customers(conn, target_count=500, memory_budget_bytes=64 * 1024, rng=random.Random(1), batch_commit_log=lambda *a: None)
    assert len(conn.tables["customers"]) == 500
    # With a tiny 64KB budget, batches must be small - confirms batch sizing
    # is actually driving executemany() calls, not just decorative.
    assert conn.peak_batch_size < 500
    assert conn.commit_count == len(conn.batch_sizes_seen["customers"])


def test_populate_customers_resumes_without_duplicating():
    conn = FakeConnection()
    psd.populate_customers(conn, target_count=300, memory_budget_bytes=1024 * 1024, rng=random.Random(2), batch_commit_log=lambda *a: None)
    assert len(conn.tables["customers"]) == 300

    # Re-run against the same (now populated) fake DB with the same target -
    # this simulates re-invoking the script after it already finished.
    psd.populate_customers(conn, target_count=300, memory_budget_bytes=1024 * 1024, rng=random.Random(2), batch_commit_log=lambda *a: None)
    assert len(conn.tables["customers"]) == 300  # not doubled


def test_populate_customers_resumes_after_partial_completion():
    conn = FakeConnection()
    # Simulate a crash after only 120 of 300 target rows were committed.
    conn.tables["customers"] = [psd.gen_customer_row(random.Random(99)) for _ in range(120)]

    psd.populate_customers(conn, target_count=300, memory_budget_bytes=1024 * 1024, rng=random.Random(3), batch_commit_log=lambda *a: None)
    assert len(conn.tables["customers"]) == 300


def test_populate_accounts_requires_customers_first():
    conn = FakeConnection()
    with pytest.raises(RuntimeError):
        psd.populate_accounts(conn, target_count=100, memory_budget_bytes=1024 * 1024, rng=random.Random(1), batch_commit_log=lambda *a: None)


def test_populate_transactions_requires_accounts_first():
    conn = FakeConnection()
    conn.tables["customers"] = [psd.gen_customer_row(random.Random(1)) for _ in range(10)]
    with pytest.raises(RuntimeError):
        psd.populate_transactions(conn, target_count=100, memory_budget_bytes=1024 * 1024, rng=random.Random(1), batch_commit_log=lambda *a: None)


def test_full_pipeline_customers_accounts_transactions():
    conn = FakeConnection()
    rng = random.Random(42)
    psd.populate_customers(conn, target_count=200, memory_budget_bytes=256 * 1024, rng=rng, batch_commit_log=lambda *a: None)
    psd.populate_accounts(conn, target_count=350, memory_budget_bytes=256 * 1024, rng=rng, batch_commit_log=lambda *a: None)
    psd.populate_transactions(conn, target_count=1000, memory_budget_bytes=256 * 1024, rng=rng, batch_commit_log=lambda *a: None)

    assert len(conn.tables["customers"]) == 200
    assert len(conn.tables["accounts"]) == 350
    assert len(conn.tables["transactions"]) == 1000

    # Every account's customer_id (1st column in gen_account_row's tuple) must
    # reference a customer_id that actually exists (1..200).
    for row in conn.tables["accounts"]:
        assert 1 <= row[0] <= 200

    # Every transaction's account_id must reference an existing account (1..350).
    for row in conn.tables["transactions"]:
        assert 1 <= row[0] <= 350


def test_estimate_transaction_target_count_is_dominant_and_positive():
    count = psd.estimate_transaction_target_count(target_total_gb=300, customer_count=200_000, account_count=360_000)
    assert count > 0
    # Sanity: at ~300GB and a few-hundred-byte row, this should be in the
    # hundreds-of-millions range, not thousands or trillions.
    assert 10_000_000 < count < 5_000_000_000


def test_generated_account_iban_is_checksum_valid():
    rng = random.Random(5)
    row = psd.gen_account_row(rng, customer_id=1)
    iban = row[4]
    rearranged = iban[4:] + iban[:4]
    numeric = ub._iban_letters_to_digits(rearranged)
    assert int(numeric) % 97 == 1


# ---------------------------------------------------------------------------
# Transaction metadata padding (the row-count/runtime control lever)
# ---------------------------------------------------------------------------

def test_metadata_none_when_zero_bytes_requested():
    assert psd.gen_transaction_metadata(random.Random(1), 0) is None


def test_metadata_is_valid_json_and_close_to_target_length():
    import json
    for target in (50, 500, 2000, 5000):
        encoded = psd.gen_transaction_metadata(random.Random(1), target)
        parsed = json.loads(encoded)  # must be valid JSON
        assert isinstance(parsed, dict)
        # Padding can't hit the target exactly (JSON escaping/structure), but
        # should land close, and never dramatically short of it.
        assert len(encoded) >= target * 0.9, f"target={target} got={len(encoded)}"


def test_transaction_row_size_scales_with_metadata():
    assert psd.transaction_row_size(0) == psd.TRANSACTION_ROW_SIZE
    assert psd.transaction_row_size(2000) == psd.TRANSACTION_ROW_SIZE + 2000


def test_estimate_transaction_target_count_shrinks_with_larger_metadata():
    thin = psd.estimate_transaction_target_count(300, 200_000, 360_000, metadata_bytes=0)
    wide = psd.estimate_transaction_target_count(300, 200_000, 360_000, metadata_bytes=2000)
    assert wide < thin
    # Roughly inversely proportional to row size (within the fudge-factor
    # noise) - confirms this lever actually moves the needle substantially,
    # not just marginally.
    assert wide < thin / 5


def test_populate_transactions_with_metadata_reaches_target_and_rows_carry_json():
    import json
    conn = FakeConnection()
    conn.tables["accounts"] = [psd.gen_account_row(random.Random(1), 1) for _ in range(50)]

    psd.populate_transactions(conn, target_count=200, memory_budget_bytes=1024 * 1024,
                               rng=random.Random(6), batch_commit_log=lambda *a: None,
                               metadata_bytes=300)

    rows = conn.tables["transactions"]
    assert len(rows) == 200
    for row in rows:
        metadata_json = row[-2]  # second-to-last column, right before transacted_at
        assert metadata_json is not None
        json.loads(metadata_json)  # must parse cleanly

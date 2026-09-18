#!/usr/bin/env python3
"""
populate_sql_database.py
Populates an Azure SQL Database with ~300GB of realistic UK-banking test
data across customers / accounts / transactions (schema.sql), mirroring the
AWS RDS schema from the earlier side of this POC.

Memory safety (the AWS-OOM-avoidance piece):
Batch size for every INSERT is computed from actual row size via
ukbank_data.calculate_batch_size(), not a flat default - see that function's
docstring for why a flat batch size is what caused the RDS OOM. Each table
gets its own row-size estimate and therefore its own batch size; transactions
(by far the largest table) gets recomputed with a slightly smaller memory
budget by default since it's the one that matters at scale.

Idempotent/resumable: before generating anything, each populate_* function
checks how many rows already exist (get_row_count) and, for tables with a
foreign key, what ID range is available to reference (get_max_id). It only
generates the shortfall, and commits after every batch, so a crash mid-run
loses at most one uncommitted batch rather than corrupting or duplicating
completed work.

Usage:
    python3 populate_sql_database.py \\
        --conn-str "Driver={ODBC Driver 18 for SQL Server};Server=tcp:<server>.database.windows.net,1433;Database=<db>;Authentication=ActiveDirectoryInteractive;Encrypt=yes;" \\
        --target-gb 300 --customers 200000 --memory-budget-mb 256
"""

import argparse
import json
import random
import string
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import ukbank_data as ub  # noqa: E402

try:
    import pyodbc
except ImportError:
    print("ERROR: pip install pyodbc (and the Microsoft ODBC Driver 18 for SQL Server)", file=sys.stderr)
    raise

SCHEMA_SQL_PATH = Path(__file__).resolve().parent / "schema.sql"

FIRST_NAMES = ["James", "Olivia", "William", "Amelia", "Thomas", "Isla", "George", "Freya",
               "Henry", "Charlotte", "Jack", "Emily", "Oscar", "Sophia", "Arthur", "Grace",
               "Muhammad", "Priya", "Aisha", "Liam", "Noah", "Ava", "Ethan", "Mia"]
LAST_NAMES = ["Smith", "Jones", "Taylor", "Williams", "Brown", "Davies", "Evans", "Wilson",
              "Thomas", "Roberts", "Johnson", "Walker", "Wright", "Robinson", "Patel", "Khan"]
TITLES = ["Mr", "Mrs", "Ms", "Miss", "Dr", "Mx"]
UK_CITIES = ["London", "Manchester", "Birmingham", "Leeds", "Glasgow", "Edinburgh",
             "Bristol", "Liverpool", "Cardiff", "Newcastle", "Nottingham", "Belfast"]
CHANNELS = ["MOBILE_APP", "ONLINE_BANKING", "BRANCH", "ATM", "TELEPHONE", "API"]


# ---------------------------------------------------------------------------
# Row-size estimates (bytes) - used to compute per-table batch sizes.
# See ukbank_data.calculate_batch_size() for why these matter.
# ---------------------------------------------------------------------------

CUSTOMER_ROW_SIZE = ub.estimate_row_size(4, 10, 12, 3, 15, 30, 8, 30, 15, 35, 12, 8)
ACCOUNT_ROW_SIZE = ub.estimate_row_size(8, 12, 8, 8, 22, 3, 8, 8, 20)
TRANSACTION_ROW_SIZE = ub.estimate_row_size(8, 10, 8, 3, 8, 8, 22, 20, 60, 30, 10, 8)  # base row, no metadata


def transaction_row_size(metadata_bytes: int = 0) -> int:
    """
    Effective transaction row size including the optional audit-metadata
    JSON field (see gen_transaction_metadata). At 300GB, the base ~220-byte
    row implies well over a billion rows - metadata_bytes is the lever for
    trading row count for row width to make that runtime tractable; see the
    README's "How long will this actually take" section.
    """
    return TRANSACTION_ROW_SIZE + metadata_bytes


# ---------------------------------------------------------------------------
# Resumability helpers
# ---------------------------------------------------------------------------

def get_row_count(cursor, table: str) -> int:
    """
    Approximate row count from catalog metadata (fast, no table scan) with a
    fallback to COUNT(*) if the metadata view isn't available/populated -
    which matters once `transactions` is large, since COUNT(*) there would
    mean scanning the whole table just to decide whether to resume.
    """
    try:
        cursor.execute(
            "SELECT SUM(row_count) FROM sys.dm_db_partition_stats "
            "WHERE object_id = OBJECT_ID(?) AND index_id IN (0, 1)",
            table,
        )
        row = cursor.fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        pass
    cursor.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cursor.fetchone()[0])


def get_max_id(cursor, table: str, id_column: str) -> int:
    cursor.execute(f"SELECT ISNULL(MAX({id_column}), 0) FROM {table}")
    return int(cursor.fetchone()[0])


# ---------------------------------------------------------------------------
# Row generators
# ---------------------------------------------------------------------------

def gen_customer_row(rng: random.Random):
    branch, sort_code = ub.gen_branch(rng)
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    dob = date(rng.randint(1945, 2004), rng.randint(1, 12), rng.randint(1, 28))
    token = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=6))
    return (
        rng.choice(TITLES),
        first,
        last,
        dob,
        rng.choice(ub.CUSTOMER_SEGMENTS),
        branch,
        sort_code,
        f"{first.lower()}.{last.lower()}.{token}@example-mail.co.uk",
        f"07{rng.randint(100000000, 999999999)}",
        f"{rng.randint(1, 200)} {rng.choice(['High Street', 'Church Road', 'Station Road', 'Mill Lane', 'Victoria Avenue'])}",
        rng.choice(UK_CITIES),
        f"{rng.choice(['SW1A', 'M1', 'B1', 'EH1', 'LS1', 'G1', 'CF10'])} {rng.randint(1, 9)}{rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}{rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}",
    )


def gen_account_row(rng: random.Random, customer_id: int):
    sort_code = ub.gen_sort_code(rng)
    account_number = ub.gen_uk_account_number(rng)
    iban = ub.gen_gb_iban(sort_code, account_number)
    opened_days_ago = rng.randint(30, 365 * 15)
    return (
        customer_id,
        rng.choice(ub.ACCOUNT_TYPES),
        sort_code,
        account_number,
        iban,
        rng.choice(ub.CURRENCIES),
        round(rng.uniform(-500.0, 50000.0), 2),
        datetime.now(timezone.utc) - timedelta(days=opened_days_ago),
        "ACTIVE",
    )


def gen_transaction_metadata(rng: random.Random, metadata_bytes: int):
    """
    Realistic-looking audit/fraud-scoring metadata as a JSON string (the kind
    of extra context a real transaction-processing pipeline logs alongside
    the core ledger fields). Returns None when metadata_bytes <= 0 (the
    default - keeps today's ~220-byte row unchanged).

    If metadata_bytes exceeds what the structured fields naturally produce,
    pads with a hex "diag" field (a device/network diagnostic trace is a
    plausible reason for a real row to carry extra raw bytes) so the encoded
    JSON lands close to the target length.
    """
    if metadata_bytes <= 0:
        return None
    payload = {
        "channel": rng.choice(CHANNELS),
        "device_id": "".join(rng.choices(string.ascii_lowercase + string.digits, k=12)),
        "ip_country": rng.choice(["GB", "GB", "GB", "GB", "IE", "FR", "DE", "US"]),
        "session_id": "".join(rng.choices(string.ascii_lowercase + string.digits, k=16)),
        "fraud_score": round(rng.uniform(0, 1), 4),
        "mcc_code": str(rng.randint(1000, 9999)),
    }
    encoded = json.dumps(payload)
    shortfall = metadata_bytes - len(encoded)
    if shortfall > 0:
        pad_field_overhead = len('"diag":""')
        pad_len = max(shortfall - pad_field_overhead, 0)
        payload["diag"] = ub.random_bytes((pad_len // 2) + 1).hex()[:pad_len]
        encoded = json.dumps(payload)
    return encoded


def gen_transaction_row(rng: random.Random, account_id: int, max_days_ago: int = 365 * 3, metadata_bytes: int = 0):
    txn_type = ub.gen_transaction_type(rng)
    amount = round(rng.uniform(-5000.0, 5000.0), 2)
    branch, counterparty_sort = ub.gen_branch(rng)
    counterparty_account = ub.gen_uk_account_number(rng)
    counterparty_iban = ub.gen_gb_iban(counterparty_sort, counterparty_account)
    transacted_at = datetime.now(timezone.utc) - timedelta(
        days=rng.randint(0, max_days_ago), seconds=rng.randint(0, 86399)
    )
    metadata_json = gen_transaction_metadata(rng, metadata_bytes)
    return (
        account_id,
        txn_type,
        amount,
        rng.choice(ub.CURRENCIES),
        counterparty_sort,
        counterparty_account,
        counterparty_iban,
        ub.gen_reference(rng),
        f"{txn_type} payment - {rng.choice(['groceries', 'salary', 'utilities', 'rent', 'transfer', 'subscription', 'fees'])}",
        branch,
        rng.choices(["SETTLED", "SETTLED", "SETTLED", "PENDING"], k=1)[0],
        metadata_json,
        transacted_at,
    )


# ---------------------------------------------------------------------------
# Population routines
# ---------------------------------------------------------------------------

def _fast_cursor(conn):
    """
    pyodbc's executemany() emulates row-by-row execution by default, which
    is dramatically slower than the SQL Server ODBC driver's native array
    binding. Setting fast_executemany=True switches to array binding and is
    a well-known, large speedup for exactly this kind of bulk-insert workload
    (Driver 18 supports it). If a different/older driver doesn't support the
    attribute, we degrade to the (slower but still correct) default.
    """
    cursor = conn.cursor()
    try:
        cursor.fast_executemany = True
    except AttributeError:
        pass
    return cursor


def populate_customers(conn, target_count: int, memory_budget_bytes: int, rng: random.Random, batch_commit_log=print):
    cursor = _fast_cursor(conn)
    existing = get_row_count(cursor, "customers")
    batch_size = ub.calculate_batch_size(CUSTOMER_ROW_SIZE, memory_budget_bytes)
    batch_commit_log(f"[customers] existing={existing} target={target_count} batch_size={batch_size}")

    remaining = target_count - existing
    insert_sql = (
        "INSERT INTO customers (title, first_name, last_name, date_of_birth, segment, "
        "home_branch, home_sort_code, email, phone, address_line1, city, postcode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    while remaining > 0:
        this_batch = min(batch_size, remaining)
        rows = [gen_customer_row(rng) for _ in range(this_batch)]
        cursor.executemany(insert_sql, rows)
        conn.commit()
        remaining -= this_batch
        batch_commit_log(f"[customers] inserted {this_batch}, {remaining} remaining")


def populate_accounts(conn, target_count: int, memory_budget_bytes: int, rng: random.Random, batch_commit_log=print):
    cursor = _fast_cursor(conn)
    existing = get_row_count(cursor, "accounts")
    max_customer_id = get_max_id(cursor, "customers", "customer_id")
    if max_customer_id == 0:
        raise RuntimeError("no customers exist yet - run populate_customers() first")

    batch_size = ub.calculate_batch_size(ACCOUNT_ROW_SIZE, memory_budget_bytes)
    batch_commit_log(f"[accounts] existing={existing} target={target_count} batch_size={batch_size} "
                      f"(customer_id range 1..{max_customer_id})")

    remaining = target_count - existing
    insert_sql = (
        "INSERT INTO accounts (customer_id, account_type, sort_code, account_number, iban, "
        "currency, balance, opened_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    while remaining > 0:
        this_batch = min(batch_size, remaining)
        rows = [gen_account_row(rng, rng.randint(1, max_customer_id)) for _ in range(this_batch)]
        cursor.executemany(insert_sql, rows)
        conn.commit()
        remaining -= this_batch
        batch_commit_log(f"[accounts] inserted {this_batch}, {remaining} remaining")


def populate_transactions(conn, target_count: int, memory_budget_bytes: int, rng: random.Random,
                           batch_commit_log=print, metadata_bytes: int = 0):
    cursor = _fast_cursor(conn)
    existing = get_row_count(cursor, "transactions")
    max_account_id = get_max_id(cursor, "accounts", "account_id")
    if max_account_id == 0:
        raise RuntimeError("no accounts exist yet - run populate_accounts() first")

    batch_size = ub.calculate_batch_size(transaction_row_size(metadata_bytes), memory_budget_bytes)
    batch_commit_log(f"[transactions] existing={existing} target={target_count} batch_size={batch_size} "
                      f"(account_id range 1..{max_account_id}, metadata_bytes={metadata_bytes})")

    remaining = target_count - existing
    insert_sql = (
        "INSERT INTO transactions (account_id, transaction_type, amount, currency, "
        "counterparty_sort_code, counterparty_account, counterparty_iban, reference, "
        "description, branch, status, metadata_json, transacted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    start = time.time()
    inserted_this_run = 0
    while remaining > 0:
        this_batch = min(batch_size, remaining)
        rows = [gen_transaction_row(rng, rng.randint(1, max_account_id), metadata_bytes=metadata_bytes)
                for _ in range(this_batch)]
        cursor.executemany(insert_sql, rows)
        conn.commit()
        remaining -= this_batch
        inserted_this_run += this_batch
        elapsed = time.time() - start
        rate = inserted_this_run / elapsed if elapsed > 0 else 0
        batch_commit_log(f"[transactions] inserted {this_batch}, {remaining} remaining, {rate:.0f} rows/s avg")


def estimate_transaction_target_count(target_total_gb: float, customer_count: int, account_count: int,
                                       metadata_bytes: int = 0) -> int:
    """
    Transactions dominate the 300GB target; back into a target row count for
    that table after subtracting the (much smaller) space customers/accounts
    are estimated to use. A 1.3x fudge factor accounts for index overhead
    on top of raw row bytes. metadata_bytes widens each row (see
    transaction_row_size()), which directly and inversely scales the row
    count needed to reach the same GB target.
    """
    target_total_bytes = ub.bytes_from_gb(target_total_gb)
    customers_bytes = customer_count * CUSTOMER_ROW_SIZE * 1.3
    accounts_bytes = account_count * ACCOUNT_ROW_SIZE * 1.3
    transactions_budget = max(target_total_bytes - customers_bytes - accounts_bytes, 0)
    return int(transactions_budget / (transaction_row_size(metadata_bytes) * 1.3))


def ensure_schema(conn):
    """
    Executes each IF NOT EXISTS ... BEGIN ... END guard block in schema.sql
    as its own batch (pyodbc doesn't support T-SQL's GO batch separator).
    Splits on a lookahead for the guard pattern rather than blank lines, so
    it's robust to header comments and to reordering/extending schema.sql
    with new tables later - only whitespace/comment-only fragments (e.g. the
    file's header) are skipped rather than executed as empty statements.
    """
    import re
    sql_text = SCHEMA_SQL_PATH.read_text()
    cursor = conn.cursor()
    for statement in re.split(r"(?=IF NOT EXISTS \(SELECT \* FROM sys\.tables)", sql_text):
        code_lines = [ln for ln in statement.splitlines() if ln.strip() and not ln.strip().startswith("--")]
        if code_lines:
            cursor.execute(statement.strip())
    conn.commit()


def run(conn, target_gb: float, customer_count: int, accounts_per_customer: float,
        memory_budget_bytes: int, seed: int = None, log=print, transaction_metadata_bytes: int = 0):
    rng = random.Random(seed)
    ensure_schema(conn)

    account_count = int(customer_count * accounts_per_customer)
    transaction_count = estimate_transaction_target_count(
        target_gb, customer_count, account_count, metadata_bytes=transaction_metadata_bytes
    )

    log(f"Targets: {customer_count:,} customers, {account_count:,} accounts, "
        f"{transaction_count:,} transactions (~{target_gb}GB total, "
        f"{transaction_row_size(transaction_metadata_bytes)}B/row)")
    if transaction_count > 50_000_000:
        # A normalized ~200-byte transaction row means "300GB" implies well
        # over a billion rows - worth surfacing loudly before a multi-hour
        # (or multi-day) job gets kicked off unattended. See README for the
        # options: run it as-is in tmux/nohup, lower --target-gb, or raise
        # --transaction-metadata-bytes to reach the same GB target with
        # materially fewer, larger rows.
        log(f"NOTE: {transaction_count:,} transaction rows at this row size will likely "
            f"take a while even with fast_executemany - see README's "
            f"'How long will this actually take' section before walking away from it. "
            f"--transaction-metadata-bytes is the lever to cut this row count down.")

    populate_customers(conn, customer_count, memory_budget_bytes, rng, batch_commit_log=log)
    populate_accounts(conn, account_count, memory_budget_bytes, rng, batch_commit_log=log)
    populate_transactions(conn, transaction_count, memory_budget_bytes, rng, batch_commit_log=log,
                           metadata_bytes=transaction_metadata_bytes)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--conn-str", required=True, help="pyodbc connection string for the target Azure SQL Database")
    ap.add_argument("--target-gb", type=float, default=300.0)
    ap.add_argument("--customers", type=int, default=200_000)
    ap.add_argument("--accounts-per-customer", type=float, default=1.8)
    ap.add_argument("--memory-budget-mb", type=int, default=256,
                     help="Per-batch memory budget in MB used to size INSERT batches "
                          "(default 256 - conservative for a modest jump VM/Cloud Shell session)")
    ap.add_argument("--transaction-metadata-bytes", type=int, default=0,
                     help="Add a realistic audit/fraud-scoring JSON field to each transaction row, "
                          "padded to roughly this many bytes. Default 0 keeps today's ~220-byte row. "
                          "Raising this trades row count for row width to reach --target-gb in "
                          "materially fewer INSERTs - e.g. 2000 cuts row count roughly 10x.")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    conn = pyodbc.connect(args.conn_str, autocommit=False)
    try:
        run(
            conn,
            target_gb=args.target_gb,
            customer_count=args.customers,
            accounts_per_customer=args.accounts_per_customer,
            memory_budget_bytes=args.memory_budget_mb * 1024 * 1024,
            seed=args.seed,
            transaction_metadata_bytes=args.transaction_metadata_bytes,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()

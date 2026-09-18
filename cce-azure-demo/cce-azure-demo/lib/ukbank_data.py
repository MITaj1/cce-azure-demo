"""
ukbank_data.py
Shared utilities for the Cohesity CCE Azure demo data generator (UK bank prospect).

Provides:
  - Fast, incompressible random byte generation (benchmarked: numpy PCG64 first,
    os.urandom fallback - see docstring on random_bytes()).
  - UK banking domain data generators: sort codes, account numbers, valid-checksum
    GB IBANs, transaction types (FPS/BACS/CHAPS/DD/SO/CARD/ATM), UK branch names,
    and the 10 business-domain names used consistently across VM roles, blob
    containers, and demo narrative.
  - JSON checkpoint/state helpers so every population script can figure out
    "what's already here" and resume without duplicating or overshooting targets.
  - A memory-aware batch-size calculator for bulk SQL inserts (this is the piece
    that matters most after the AWS RDS OOM - see calculate_batch_size()).

This module has no Azure dependencies - it is pure Python (+ numpy, which is
common enough to not worry about) so it can be imported and unit-tested without
any cloud SDKs or live resources.
"""

from __future__ import annotations

import json
import os
import random
import string
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    import numpy as _np
    _RNG = _np.random.Generator(_np.random.PCG64())
    _HAVE_NUMPY = True
except ImportError:  # pragma: no cover - numpy is a listed dependency, but degrade gracefully
    _HAVE_NUMPY = False


# ---------------------------------------------------------------------------
# Fast incompressible random bytes
# ---------------------------------------------------------------------------
#
# Benchmarked in the delivery sandbox on a 512MB sample (see tests/ for the
# repeatable version of this check):
#     os.urandom     ~400 MB/s
#     numpy PCG64    ~610 MB/s   (~50% faster)
# Both compress to ~100.03% of their original size under zlib -6 (i.e. they do
# not compress at all) - PCG64 is not cryptographically secure, but that is
# irrelevant for test-data purposes and it is meaningfully faster at TB scale.

def random_bytes(n: int) -> bytes:
    """Return n high-entropy, incompressible random bytes as fast as possible."""
    if _HAVE_NUMPY:
        return _RNG.bytes(n)
    return os.urandom(n)


def write_random_file(path, size_bytes: int, chunk_size: int = 64 * 1024 * 1024) -> int:
    """
    Write exactly size_bytes of incompressible random data to `path`, streamed
    in `chunk_size` chunks so memory use stays flat regardless of file size.
    Returns the number of bytes written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(path, "wb") as f:
        while written < size_bytes:
            this_chunk = min(chunk_size, size_bytes - written)
            f.write(random_bytes(this_chunk))
            written += this_chunk
    return written


# ---------------------------------------------------------------------------
# UK banking domain constants
# ---------------------------------------------------------------------------

# Ten business domains - used consistently as: VM role names, blob container
# names (mirrors the "bucket-per-domain" approach from the AWS side), and as
# a natural grouping for the SQL schema's narrative. 10 domains x 1 VM each
# maps cleanly onto the 10 VMs requested.
BUSINESS_DOMAINS = [
    "core-banking",
    "payments-hub",
    "cards-processing",
    "aml-compliance",
    "risk-analytics",
    "wealth-management",
    "mobile-banking",
    "branch-systems",
    "data-warehouse",
    "dr-replica",
]

UK_BRANCHES = [
    ("London Canary Wharf", "20"), ("London City", "30"), ("Edinburgh St Andrew Square", "83"),
    ("Manchester King Street", "40"), ("Birmingham Colmore Row", "60"), ("Leeds Park Row", "16"),
    ("Bristol Corn Street", "16"), ("Glasgow Buchanan Street", "83"), ("Liverpool Water Street", "40"),
    ("Cardiff Queen Street", "40"), ("Newcastle Grey Street", "55"), ("Nottingham Old Market Square", "60"),
    ("Leicester Market Street", "60"), ("Southampton Above Bar", "30"), ("Belfast Donegall Square", "95"),
]

TRANSACTION_TYPES = ["FPS", "BACS", "CHAPS", "DD", "SO", "CARD", "ATM"]
TRANSACTION_TYPE_WEIGHTS = [30, 20, 5, 15, 10, 15, 5]  # roughly realistic mix

CURRENCIES = ["GBP", "GBP", "GBP", "GBP", "EUR", "USD"]  # mostly GBP, some FX for realism

ACCOUNT_TYPES = ["CURRENT", "SAVINGS", "BUSINESS", "ISA", "MORTGAGE_OFFSET"]

CUSTOMER_SEGMENTS = ["RETAIL", "PREMIER", "BUSINESS", "CORPORATE", "PRIVATE_BANKING"]

_BANK_CODE_LETTERS = "COHE"  # fictional 4-letter bank code used in generated IBANs


# ---------------------------------------------------------------------------
# UK banking data generators
# ---------------------------------------------------------------------------

def gen_sort_code(rng: random.Random = random) -> str:
    """Return a UK sort code formatted NN-NN-NN."""
    return f"{rng.randint(10, 99)}-{rng.randint(10, 99)}-{rng.randint(10, 99)}"


def gen_uk_account_number(rng: random.Random = random) -> str:
    """Return an 8-digit UK bank account number, zero-padded."""
    return f"{rng.randint(0, 99999999):08d}"


def _iban_letters_to_digits(s: str) -> str:
    """Map A-Z -> 10-35 (per ISO 7064 mod-97-10) and pass digits through."""
    out = []
    for c in s:
        if c.isalpha():
            out.append(str(int(c, 36)))  # base-36: a=10 ... z=35
        else:
            out.append(c)
    return "".join(out)


def gen_gb_iban(sort_code: str, account_number: str, bank_code: str = _BANK_CODE_LETTERS) -> str:
    """
    Return a check-digit-valid GB IBAN (ISO 7064 mod-97-10), 22 characters:
    GB + 2 check digits + 4-letter bank code + 6-digit sort code + 8-digit account.
    bank_code/sort_code/account are fictional but the checksum is real, so the
    IBANs will pass basic IBAN validators used in demo tooling.
    """
    sort_digits = sort_code.replace("-", "")
    bban = f"{bank_code}{sort_digits}{account_number}"  # 4 + 6 + 8 = 18 chars
    rearranged = f"{bban}GB00"
    numeric = _iban_letters_to_digits(rearranged)
    check = 98 - (int(numeric) % 97)
    return f"GB{check:02d}{bban}"


def gen_branch(rng: random.Random = random):
    """Return (branch_name, sort_code) for a randomly chosen UK branch."""
    name, prefix = rng.choice(UK_BRANCHES)
    sort_code = f"{prefix}-{rng.randint(10, 99)}-{rng.randint(10, 99)}"
    return name, sort_code


def gen_transaction_type(rng: random.Random = random) -> str:
    return rng.choices(TRANSACTION_TYPES, weights=TRANSACTION_TYPE_WEIGHTS, k=1)[0]


def gen_reference(rng: random.Random = random) -> str:
    """A short alphanumeric payment reference, e.g. as seen on a bank statement."""
    words = ["INV", "SAL", "RENT", "PAYMENT", "REFUND", "TRF", "SUPPLIER", "PENSION", "DIVIDEND"]
    suffix = "".join(rng.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{rng.choice(words)}-{suffix}"


def gen_domain_filename(domain: str, rng: random.Random = random, ext: str = "dat", dt: Optional[datetime] = None) -> str:
    """
    A realistic-looking file/blob name for a given business domain, e.g.
    'core-banking/2026/09/core-banking_extract_20260917_143022_a1b2c3.dat'
    """
    dt = dt or datetime.now(timezone.utc)
    stamp = dt.strftime("%Y%m%d_%H%M%S")
    token = "".join(rng.choices(string.ascii_lowercase + string.digits, k=6))
    kind = rng.choice(["extract", "batch", "ledger", "export", "snapshot", "audit_log"])
    return f"{domain}/{dt.year:04d}/{dt.month:02d}/{domain}_{kind}_{stamp}_{token}.{ext}"


# ---------------------------------------------------------------------------
# Checkpoint / resume-state helpers
# ---------------------------------------------------------------------------

@dataclass
class CheckpointStore:
    """
    Minimal JSON-backed state file so a population script can be killed at any
    point and re-run without redoing (or overshooting) completed work.

    Usage pattern used by every script in this project:
        cp = CheckpointStore(path)
        done_bytes = cp.get("bytes_written", 0)
        while done_bytes < target_bytes:
            ... do one unit of work ...
            done_bytes += unit_size
            cp.set("bytes_written", done_bytes)   # persisted immediately
    """
    path: Path
    _data: dict = field(default_factory=dict)

    def __post_init__(self):
        self.path = Path(self.path)
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        self._flush()

    def update(self, **kwargs):
        self._data.update(kwargs)
        self._flush()

    def _flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically: temp file + rename, so a crash mid-write never
        # corrupts the checkpoint that resumability depends on.
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), prefix=".ckpt_tmp_")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f)
            os.replace(tmp_path, self.path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def as_dict(self) -> dict:
        return dict(self._data)


# ---------------------------------------------------------------------------
# Batch-size calculator (the AWS-OOM-avoidance piece)
# ---------------------------------------------------------------------------

def calculate_batch_size(
    avg_row_size_bytes: float,
    memory_budget_bytes: int = 256 * 1024 * 1024,
    overhead_multiplier: float = 4.0,
    min_batch: int = 50,
    max_batch: int = 50_000,
) -> int:
    """
    Compute a conservative executemany() batch size from row size rather than
    using a flat default.

    Why overhead_multiplier defaults to 4x: the failure on the AWS RDS side
    came from sizing batches off raw column-byte counts alone. In practice, a
    batch held in memory before executemany() carries much more than that:
      - CPython object overhead per str/int/Decimal/tuple (measured directly
        via tracemalloc against this project's actual row generators: ~2.2x
        to 2.9x raw field-byte counts for the customers/accounts/transactions
        row shapes - see tests/test_populate_sql_database.py),
      - the DB driver's own parameter-marshalling buffers, and
      - on constrained instances, the OS/network send buffer duplicating the
        same data again during the round trip - neither of which shows up in
        a plain Python-object memory measurement.
    4x sits comfortably above the measured 2.2-2.9x Python-side figure,
    leaving headroom for those last two, driver-and-network layers.

    memory_budget_bytes is how much of that overhead-inflated memory you are
    willing to hold in one batch (default 256MB - conservative for a modest
    Cloud Shell session or small jump VM; lower it if you know the box is
    tighter than that, e.g. the instance size that produced the AWS OOM).

    max_batch is a safety/performance ceiling independent of memory (holding
    a lock and growing the transaction log for an extremely large single
    batch has its own costs even when memory technically allows it) - 50,000
    is well within normal bulk-insert guidance for SQL Server. min_batch
    keeps a batch from rounding down to zero when rows are unusually large.
    For typical banking-row sizes (a few hundred bytes) at a generous memory
    budget, max_batch is the practical ceiling; it's the memory_budget term
    that does the protective work once the budget gets tight - which is
    exactly the regime the original OOM happened in.
    """
    if avg_row_size_bytes <= 0:
        raise ValueError("avg_row_size_bytes must be positive")
    effective_row_size = avg_row_size_bytes * overhead_multiplier
    raw_batch = int(memory_budget_bytes // effective_row_size)
    return max(min_batch, min(max_batch, max(raw_batch, 1)))


def estimate_row_size(*field_lengths: int, fixed_overhead: int = 24) -> int:
    """
    Rough estimate of a row's on-the-wire size given the byte length of each
    field's typical value, plus a small fixed per-row overhead (row header,
    nulls bitmap, etc.). Deliberately simple - it only needs to be in the
    right ballpark for calculate_batch_size() to size batches sensibly.
    """
    return sum(field_lengths) + fixed_overhead


# ---------------------------------------------------------------------------
# Disk-usage helpers (used by the VM data-disk and blob population scripts)
# ---------------------------------------------------------------------------

def bytes_from_gb(gb: float) -> int:
    return int(gb * 1024 * 1024 * 1024)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PB"

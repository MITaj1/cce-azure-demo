import json
import re
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import ukbank_data as ub


def test_sort_code_format():
    for _ in range(200):
        sc = ub.gen_sort_code()
        assert re.fullmatch(r"\d{2}-\d{2}-\d{2}", sc), sc


def test_account_number_format():
    for _ in range(200):
        acc = ub.gen_uk_account_number()
        assert re.fullmatch(r"\d{8}", acc), acc


def test_iban_length_and_prefix():
    for _ in range(200):
        sc = ub.gen_sort_code()
        acc = ub.gen_uk_account_number()
        iban = ub.gen_gb_iban(sc, acc)
        assert len(iban) == 22, iban
        assert iban.startswith("GB"), iban


def test_iban_checksum_matches_known_real_world_example():
    # Published real-world example IBAN used ubiquitously in IBAN-validator
    # test suites: GB29 NWBK 6016 1331 9268 19
    # bank code NWBK, sort code 601613, account 31926819, check digits 29.
    iban = ub.gen_gb_iban(sort_code="60-16-13", account_number="31926819", bank_code="NWBK")
    assert iban == "GB29NWBK60161331926819", iban


def test_iban_checksum_is_internally_self_consistent():
    # For any IBAN we generate, mod-97 of the full rearranged number must be 1
    # (this is the defining property of a valid ISO 7064 mod-97-10 checksum).
    for _ in range(500):
        sc = ub.gen_sort_code()
        acc = ub.gen_uk_account_number()
        iban = ub.gen_gb_iban(sc, acc)
        rearranged = iban[4:] + iban[:4]
        numeric = ub._iban_letters_to_digits(rearranged)
        assert int(numeric) % 97 == 1, iban


def test_transaction_type_within_allowed_set():
    for _ in range(200):
        assert ub.gen_transaction_type() in ub.TRANSACTION_TYPES


def test_domain_filename_structure():
    name = ub.gen_domain_filename("payments-hub")
    assert name.startswith("payments-hub/")
    assert name.endswith(".dat")
    parts = name.split("/")
    assert len(parts) == 4  # domain / year / month / filename


def test_business_domains_count_matches_vm_count():
    # 10 VMs requested -> 1:1 mapping onto business domains
    assert len(ub.BUSINESS_DOMAINS) == 10
    assert len(set(ub.BUSINESS_DOMAINS)) == 10  # all unique


# --- random byte generation -------------------------------------------------

def test_random_bytes_length_exact():
    data = ub.random_bytes(12345)
    assert len(data) == 12345


def test_random_bytes_incompressible():
    # Real random data should not meaningfully compress. A repeating pattern
    # of the same size would compress to a tiny fraction; genuine random
    # bytes should compress to ~100% (zlib framing adds a few bytes).
    data = ub.random_bytes(4 * 1024 * 1024)
    compressed = zlib.compress(data, level=6)
    ratio = len(compressed) / len(data)
    assert ratio > 0.99, f"data compressed too well (ratio={ratio:.4f}) - not sufficiently random"


def test_random_bytes_differs_between_calls():
    a = ub.random_bytes(1024)
    b = ub.random_bytes(1024)
    assert a != b


def test_write_random_file_exact_size(tmp_path):
    target = tmp_path / "sub" / "file.dat"
    size = 5 * 1024 * 1024 + 777  # not a round chunk multiple, exercises the tail chunk
    written = ub.write_random_file(target, size, chunk_size=1024 * 1024)
    assert written == size
    assert target.stat().st_size == size


# --- batch size calculator ---------------------------------------------------

def test_batch_size_respects_min_and_max():
    # Absurdly tiny rows should clamp to max_batch, not blow up.
    assert ub.calculate_batch_size(1, max_batch=5000) == 5000
    # Absurdly huge rows should clamp to min_batch, not go to zero.
    assert ub.calculate_batch_size(10_000_000, min_batch=50) == 50


def test_batch_size_realistic_transactions_row():
    # Roughly the shape of the transactions row used in populate_sql_database.py.
    # At this row size, a generous 256MB budget is well above what's needed,
    # so the max_batch ceiling (not the memory term) is expected to bind -
    # that's correct: the memory term is what protects a *constrained* box,
    # exercised separately below with a tight budget.
    row_size = ub.estimate_row_size(8, 8, 8, 8, 3, 5, 8, 8, 22, 20, 60, 30, 8)
    generous_batch = ub.calculate_batch_size(row_size, memory_budget_bytes=256 * 1024 * 1024)
    assert generous_batch == 50_000  # hits the ceiling at a generous budget

    tight_batch = ub.calculate_batch_size(row_size, memory_budget_bytes=4 * 1024 * 1024)
    assert 100 <= tight_batch < generous_batch  # a tight budget genuinely constrains it


def test_batch_size_scales_inversely_with_row_size():
    # Use row sizes large enough that neither result clamps to max_batch,
    # so the inverse relationship is actually exercised.
    small = ub.calculate_batch_size(20_000)
    large = ub.calculate_batch_size(40_000)
    assert small > large


def test_batch_size_rejects_nonpositive_row_size():
    import pytest
    with pytest.raises(ValueError):
        ub.calculate_batch_size(0)


# --- checkpoint store ---------------------------------------------------------

def test_checkpoint_store_roundtrip(tmp_path):
    cp_path = tmp_path / "state.json"
    cp = ub.CheckpointStore(cp_path)
    assert cp.get("bytes_written", 0) == 0
    cp.set("bytes_written", 12345)
    cp.update(last_file="foo.dat", done=False)

    # Simulate a fresh process re-reading the same checkpoint file.
    cp2 = ub.CheckpointStore(cp_path)
    assert cp2.get("bytes_written") == 12345
    assert cp2.get("last_file") == "foo.dat"
    assert cp2.get("done") is False


def test_checkpoint_store_survives_missing_file(tmp_path):
    cp = ub.CheckpointStore(tmp_path / "does_not_exist_yet.json")
    assert cp.get("anything", "default") == "default"


def test_checkpoint_store_atomic_write_leaves_no_temp_files(tmp_path):
    cp_path = tmp_path / "state.json"
    cp = ub.CheckpointStore(cp_path)
    cp.set("x", 1)
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".ckpt_tmp_")]
    assert leftovers == []
    assert json.loads(cp_path.read_text()) == {"x": 1}

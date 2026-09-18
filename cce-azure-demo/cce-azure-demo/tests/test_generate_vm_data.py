import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vm_disks"))

import ukbank_data as ub  # noqa: E402
import generate_vm_data as gvd  # noqa: E402


def _total_data_bytes(mount_path: Path) -> int:
    total = 0
    for p in mount_path.rglob("*"):
        if p.is_file() and p.name != gvd.MANIFEST_NAME:
            total += p.stat().st_size
    return total


def test_fresh_run_reaches_target(tmp_path):
    target = ub.bytes_from_gb(0.01)  # 10MB - keep the test fast
    gvd.run(tmp_path, "core-banking", target, seed=1)
    assert _total_data_bytes(tmp_path) == target

    manifest = ub.CheckpointStore(tmp_path / gvd.MANIFEST_NAME)
    assert sum(manifest.get("files", {}).values()) == target


def test_resume_does_not_duplicate_or_overshoot(tmp_path, capsys):
    target = ub.bytes_from_gb(0.01)
    gvd.run(tmp_path, "core-banking", target, seed=1)
    files_after_first_run = dict(ub.CheckpointStore(tmp_path / gvd.MANIFEST_NAME).get("files"))

    # Re-run against the same directory/target - this simulates re-running
    # after an interruption. Nothing should be regenerated or duplicated.
    gvd.run(tmp_path, "core-banking", target, seed=1)
    capsys.readouterr()

    files_after_second_run = dict(ub.CheckpointStore(tmp_path / gvd.MANIFEST_NAME).get("files"))
    assert files_after_first_run == files_after_second_run
    assert _total_data_bytes(tmp_path) == target  # not doubled


def test_resume_after_partial_interruption_completes_target(tmp_path):
    # Simulate a script that got killed partway: manually write a manifest
    # recording only some of the target as already done, with a real file
    # backing each entry, then confirm the resumed run tops up to the target
    # rather than starting over or overshooting.
    target = ub.bytes_from_gb(0.02)  # 20MB
    partial = ub.bytes_from_gb(0.005)  # 5MB already "done"

    manifest = ub.CheckpointStore(tmp_path / gvd.MANIFEST_NAME)
    rel_path = "core-banking/2026/09/core-banking_extract_partial.dat"
    ub.write_random_file(tmp_path / rel_path, partial)
    manifest.set("files", {rel_path: partial})

    gvd.run(tmp_path, "core-banking", target, seed=2)

    assert _total_data_bytes(tmp_path) == target


def test_manifest_self_heals_when_file_deleted_externally(tmp_path):
    # If a file the manifest thinks exists was deleted (e.g. someone cleaned
    # up disk space manually), the script should notice and regenerate to
    # reach target rather than trusting a stale manifest.
    target = ub.bytes_from_gb(0.01)
    gvd.run(tmp_path, "core-banking", target, seed=3)

    manifest = ub.CheckpointStore(tmp_path / gvd.MANIFEST_NAME)
    files = manifest.get("files")
    some_rel_path = next(iter(files))
    (tmp_path / some_rel_path).unlink()

    gvd.run(tmp_path, "core-banking", target, seed=3)
    assert _total_data_bytes(tmp_path) == target


def test_already_met_target_is_a_noop(tmp_path, capsys):
    target = ub.bytes_from_gb(0.01)
    gvd.run(tmp_path, "core-banking", target, seed=4)
    before = _total_data_bytes(tmp_path)
    mtimes_before = {p: p.stat().st_mtime for p in tmp_path.rglob("*") if p.is_file()}

    gvd.run(tmp_path, "core-banking", target, seed=4)
    out = capsys.readouterr().out
    assert "nothing to do" in out

    after = _total_data_bytes(tmp_path)
    mtimes_after = {p: p.stat().st_mtime for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after
    assert mtimes_before == mtimes_after  # confirms files weren't rewritten

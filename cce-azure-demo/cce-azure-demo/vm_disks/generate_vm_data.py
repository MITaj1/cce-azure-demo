#!/usr/bin/env python3
"""
generate_vm_data.py
Runs ON a demo VM (not from your workstation) to fill that VM's mounted data
disk with realistic-looking, incompressible UK-banking test data.

Idempotent/resumable: on every run it first scans the target directory for
files already recorded in the local manifest, verifies they're still the
expected size, and only generates what's missing to reach the target. Safe
to Ctrl-C and re-run, or to lose the session (SSH drop, VM reboot) and re-run
later - see the orchestration script and README for how this gets invoked
without needing a persistent shell.

Usage:
    python3 generate_vm_data.py --mount /mnt/datadisk --domain core-banking --target-gb 90

--domain should be one of the 10 BUSINESS_DOMAINS in ukbank_data.py (each of
the 10 VMs is assigned one, matching its role in the demo narrative and the
blob storage container of the same name).
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import ukbank_data as ub  # noqa: E402

MANIFEST_NAME = ".cce_manifest.json"
# File sizes chosen to look like a plausible mix of banking extracts/exports
# rather than one giant blob - varied dedup/compression behaviour is part of
# what makes the backup-size demo numbers look realistic.
FILE_SIZE_CHOICES_MB = [50, 100, 250, 500, 1024, 2048]


def scan_existing(mount_path: Path, manifest: ub.CheckpointStore) -> int:
    """
    Verify every file the manifest thinks it wrote is still present and the
    right size; drop anything that's missing/wrong-sized from the manifest so
    it gets regenerated, and return the confirmed-good byte total.
    """
    files = manifest.get("files", {})
    confirmed_total = 0
    changed = False
    for rel_path, expected_size in list(files.items()):
        full_path = mount_path / rel_path
        if full_path.exists() and full_path.stat().st_size == expected_size:
            confirmed_total += expected_size
        else:
            del files[rel_path]
            changed = True
    if changed:
        manifest.set("files", files)
    return confirmed_total


def run(mount_path: Path, domain: str, target_bytes: int, seed: int = None) -> None:
    import random
    rng = random.Random(seed)

    mount_path.mkdir(parents=True, exist_ok=True)
    manifest = ub.CheckpointStore(mount_path / MANIFEST_NAME)

    confirmed_bytes = scan_existing(mount_path, manifest)
    print(f"[{domain}] already present and verified: {ub.human_bytes(confirmed_bytes)} "
          f"of {ub.human_bytes(target_bytes)} target")

    files = manifest.get("files", {})
    remaining = target_bytes - confirmed_bytes
    if remaining <= 0:
        print(f"[{domain}] target already met, nothing to do")
        return

    start = time.time()
    written_this_run = 0
    while remaining > 0:
        size_mb = rng.choice(FILE_SIZE_CHOICES_MB)
        size_bytes = min(size_mb * 1024 * 1024, remaining)
        rel_path = ub.gen_domain_filename(domain, rng=rng, ext=rng.choice(["dat", "csv", "log", "bak"]))
        full_path = mount_path / rel_path

        ub.write_random_file(full_path, size_bytes)

        files[rel_path] = size_bytes
        manifest.set("files", files)  # persisted immediately - safe to interrupt here

        written_this_run += size_bytes
        remaining -= size_bytes

        elapsed = time.time() - start
        rate = written_this_run / elapsed / (1024 * 1024) if elapsed > 0 else 0
        print(f"[{domain}] wrote {rel_path} ({ub.human_bytes(size_bytes)}) "
              f"- {ub.human_bytes(remaining)} remaining - {rate:.0f} MB/s avg")

    print(f"[{domain}] done. Total on disk: {ub.human_bytes(target_bytes)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mount", required=True, help="Mount point of the data disk, e.g. /mnt/datadisk")
    ap.add_argument("--domain", required=True, choices=ub.BUSINESS_DOMAINS,
                     help="Business domain this VM represents")
    ap.add_argument("--target-gb", type=float, default=90.0,
                     help="Target GB to fill (default 90 - leaves headroom on a 100GB disk)")
    ap.add_argument("--seed", type=int, default=None, help="Optional RNG seed for reproducible runs")
    args = ap.parse_args()

    run(Path(args.mount), args.domain, ub.bytes_from_gb(args.target_gb), seed=args.seed)


if __name__ == "__main__":
    main()

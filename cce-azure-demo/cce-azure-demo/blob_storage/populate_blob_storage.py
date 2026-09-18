#!/usr/bin/env python3
"""
populate_blob_storage.py
Fills an Azure Storage account with ~1TB of realistic-looking, incompressible
UK-banking test data, spread across 10 containers named after the same
business domains used for the VM data disks (bucket-per-domain, mirroring
the S3 layout from the AWS side of this POC).

Idempotent/resumable: before uploading anything, it lists what's already in
each container and sums the sizes, so re-running after an interruption tops
up to the target instead of re-uploading or overshooting. Blob names are
also checked individually (name + size) before upload so a partially-run
batch never double-uploads.

Auth: two ways to authenticate, pick whichever's already available to you -
  --account-key or --connection-string: the storage account's access key
    (Portal: storage account -> Access keys) or full connection string. No
    login flow, no CLI install, works from any box with no other setup -
    the simplest option in a locked-down environment with no managed
    identity and no `az` installed.
  --account-name alone (no key/conn-str given): falls back to
    DefaultAzureCredential, which needs a managed identity OR a prior
    `az login` on this machine. Use this on a box that already has one of
    those set up.

Usage:
    python3 populate_blob_storage.py \\
        --account-name cceukbankdemo --account-key "<key from Portal>" \\
        --target-tb 1.0

Run from a jump VM or Cloud Shell, ideally inside tmux/screen (see README)
since this is a multi-hour upload at 1TB.
"""

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import ukbank_data as ub  # noqa: E402

try:
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient, ContentSettings
except ImportError:
    print("ERROR: pip install azure-storage-blob azure-identity", file=sys.stderr)
    raise

# Upload in chunks that are large enough to be efficient but small enough to
# cap memory use - each chunk is generated fresh (not read from disk) so
# there's no local TB-scale staging file needed.
UPLOAD_CHUNK_MB = 64
BLOB_SIZE_CHOICES_MB = [50, 100, 250, 500, 1024, 2048, 4096]


class RandomDataStream:
    """
    A file-like object that yields incompressible random bytes on read(),
    up to a fixed total length, without ever materializing the whole blob
    in memory. Lets the Azure SDK's chunked upload_blob() stream it directly.
    """
    def __init__(self, total_size: int, chunk_size: int = UPLOAD_CHUNK_MB * 1024 * 1024):
        self.total_size = total_size
        self.chunk_size = chunk_size
        self._remaining = total_size

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        n = self._remaining if size in (-1, None) else min(size, self._remaining)
        self._remaining -= n
        return ub.random_bytes(n)

    def __len__(self):
        return self.total_size


def existing_bytes_per_container(blob_service: BlobServiceClient, container_name: str) -> tuple[int, set]:
    """Return (total bytes already in container, set of existing blob names)."""
    container_client = blob_service.get_container_client(container_name)
    total = 0
    names = set()
    for blob in container_client.list_blobs():
        total += blob.size
        names.add(blob.name)
    return total, names


def ensure_container(blob_service: BlobServiceClient, container_name: str):
    container_client = blob_service.get_container_client(container_name)
    if not container_client.exists():
        container_client.create_container()
    return container_client


def populate_container(blob_service: BlobServiceClient, domain: str, target_bytes: int, rng: random.Random):
    container_client = ensure_container(blob_service, domain)
    existing_total, existing_names = existing_bytes_per_container(blob_service, domain)

    print(f"[{domain}] already present: {ub.human_bytes(existing_total)} of {ub.human_bytes(target_bytes)} target")
    remaining = target_bytes - existing_total
    if remaining <= 0:
        print(f"[{domain}] target already met, nothing to do")
        return

    start = time.time()
    uploaded_this_run = 0
    while remaining > 0:
        size_mb = rng.choice(BLOB_SIZE_CHOICES_MB)
        size_bytes = min(size_mb * 1024 * 1024, remaining)

        # Regenerate the name on collision rather than trusting it's unique -
        # gen_domain_filename() includes a timestamp + random token so
        # collisions are extremely unlikely, but a resumed run must never
        # silently skip real work because of one.
        blob_name = ub.gen_domain_filename(domain, rng=rng, ext=rng.choice(["dat", "csv", "log", "bak"]))
        while blob_name in existing_names:
            blob_name = ub.gen_domain_filename(domain, rng=rng, ext=rng.choice(["dat", "csv", "log", "bak"]))

        stream = RandomDataStream(size_bytes)
        container_client.upload_blob(
            name=blob_name,
            data=stream,
            length=size_bytes,
            overwrite=False,
            content_settings=ContentSettings(content_type="application/octet-stream"),
        )
        existing_names.add(blob_name)

        uploaded_this_run += size_bytes
        remaining -= size_bytes
        elapsed = time.time() - start
        rate = uploaded_this_run / elapsed / (1024 * 1024) if elapsed > 0 else 0
        print(f"[{domain}] uploaded {blob_name} ({ub.human_bytes(size_bytes)}) "
              f"- {ub.human_bytes(remaining)} remaining - {rate:.1f} MB/s avg")

    print(f"[{domain}] done. Total in container: ~{ub.human_bytes(target_bytes)}")


def build_blob_service_client(account_name: str, account_key: str = None, connection_string: str = None):
    """
    Three ways in, tried in this order: an explicit connection string, an
    account name + key (Portal: storage account -> Access keys - the
    simplest path with no managed identity and no `az login`), or falling
    back to DefaultAzureCredential (needs one of those already set up).
    """
    if connection_string:
        return BlobServiceClient.from_connection_string(connection_string)
    account_url = f"https://{account_name}.blob.core.windows.net"
    if account_key:
        return BlobServiceClient(account_url=account_url, credential=account_key)
    return BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())


def run(account_name: str, target_bytes: int, credential=None, seed: int = None,
        account_key: str = None, connection_string: str = None):
    rng = random.Random(seed)
    if credential is not None:
        account_url = f"https://{account_name}.blob.core.windows.net"
        blob_service = BlobServiceClient(account_url=account_url, credential=credential)
    else:
        blob_service = build_blob_service_client(account_name, account_key, connection_string)

    per_domain_target = target_bytes // len(ub.BUSINESS_DOMAINS)
    for domain in ub.BUSINESS_DOMAINS:
        populate_container(blob_service, domain, per_domain_target, rng)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-name", required=True, help="Azure Storage account name (no domain suffix)")
    ap.add_argument("--account-key", default=None,
                     help="Storage account access key (Portal: storage account -> Access keys). "
                          "Simplest auth path with no managed identity and no az login available.")
    ap.add_argument("--connection-string", default=None,
                     help="Full storage connection string, as an alternative to --account-key.")
    ap.add_argument("--target-tb", type=float, default=1.0, help="Total target size across all containers, in TB")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    target_bytes = int(args.target_tb * 1024 * 1024 * 1024 * 1024)
    run(args.account_name, target_bytes, seed=args.seed,
        account_key=args.account_key, connection_string=args.connection_string)


if __name__ == "__main__":
    main()

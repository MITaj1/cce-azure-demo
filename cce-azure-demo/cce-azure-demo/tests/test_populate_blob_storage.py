import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "blob_storage"))

import ukbank_data as ub  # noqa: E402
import populate_blob_storage as pbs  # noqa: E402


# ---------------------------------------------------------------------------
# In-memory fakes standing in for azure.storage.blob's BlobServiceClient /
# ContainerClient, since no real Azure endpoint is reachable in this sandbox.
# These implement just enough of the real interface for our script to drive.
# ---------------------------------------------------------------------------

class FakeBlob:
    def __init__(self, name, size):
        self.name = name
        self.size = size


class FakeContainerClient:
    def __init__(self, store: dict, container_name: str):
        self._store = store  # shared dict: container_name -> {blob_name: size}
        self.container_name = container_name
        self._store.setdefault(container_name, {})

    def exists(self):
        return True  # ensure_container calls create_container() only if False

    def create_container(self):
        self._store.setdefault(self.container_name, {})

    def list_blobs(self):
        return [FakeBlob(name, size) for name, size in self._store[self.container_name].items()]

    def upload_blob(self, name, data, length, overwrite=False, content_settings=None):
        if not overwrite and name in self._store[self.container_name]:
            raise FileExistsError(f"blob {name} already exists")
        # Actually drain the stream (like the real SDK would) so we exercise
        # RandomDataStream's read() logic, but don't keep the bytes - we only
        # need to confirm the total length matches `length`.
        total_read = 0
        while True:
            chunk = data.read(8 * 1024 * 1024)
            if not chunk:
                break
            total_read += len(chunk)
        assert total_read == length, f"stream produced {total_read} bytes, expected {length}"
        self._store[self.container_name][name] = length


class FakeBlobServiceClient:
    """Drop-in stand-in for azure.storage.blob.BlobServiceClient."""
    def __init__(self, account_url=None, credential=None, initial_store=None):
        self.account_url = account_url
        self._store = initial_store if initial_store is not None else {}

    def get_container_client(self, container_name):
        return FakeContainerClient(self._store, container_name)


# ---------------------------------------------------------------------------

def test_fresh_population_reaches_target_across_all_domains():
    fake = FakeBlobServiceClient(initial_store={})
    target_bytes = ub.bytes_from_gb(0.05) * len(ub.BUSINESS_DOMAINS)  # keep test fast: 50MB/domain
    rng = random.Random(1)

    per_domain_target = target_bytes // len(ub.BUSINESS_DOMAINS)
    for domain in ub.BUSINESS_DOMAINS:
        pbs.populate_container(fake, domain, per_domain_target, rng)

    for domain in ub.BUSINESS_DOMAINS:
        total = sum(fake._store[domain].values())
        assert total == per_domain_target, f"{domain}: got {total}, want {per_domain_target}"


def test_resume_tops_up_without_duplicating():
    per_domain_target = ub.bytes_from_gb(0.05)
    # Pre-seed the fake store as if a previous run already uploaded some data.
    pre_existing = {"core-banking/2026/09/core-banking_extract_partial.dat": ub.bytes_from_gb(0.02)}
    fake = FakeBlobServiceClient(initial_store={"core-banking": dict(pre_existing)})
    rng = random.Random(2)

    pbs.populate_container(fake, "core-banking", per_domain_target, rng)

    total = sum(fake._store["core-banking"].values())
    assert total == per_domain_target
    # The pre-existing blob must still be there, untouched, not re-uploaded.
    assert fake._store["core-banking"]["core-banking/2026/09/core-banking_extract_partial.dat"] == pre_existing[
        "core-banking/2026/09/core-banking_extract_partial.dat"
    ]


def test_already_met_target_is_a_noop(capsys):
    per_domain_target = ub.bytes_from_gb(0.03)
    fake = FakeBlobServiceClient(initial_store={"core-banking": {"already-full.dat": per_domain_target}})
    rng = random.Random(3)

    pbs.populate_container(fake, "core-banking", per_domain_target, rng)

    out = capsys.readouterr().out
    assert "nothing to do" in out
    assert sum(fake._store["core-banking"].values()) == per_domain_target


def test_random_data_stream_produces_exact_length_and_is_incompressible():
    import zlib
    size = 3 * 1024 * 1024 + 123  # not a round chunk multiple
    stream = pbs.RandomDataStream(size, chunk_size=1024 * 1024)
    collected = b""
    while True:
        chunk = stream.read(512 * 1024)
        if not chunk:
            break
        collected += chunk
    assert len(collected) == size
    ratio = len(zlib.compress(collected, level=6)) / len(collected)
    assert ratio > 0.99


def test_full_run_across_all_domains_via_run_function(monkeypatch):
    # Exercise run() end-to-end (account URL construction, credential
    # plumbing, per-domain split) with BlobServiceClient patched to our fake.
    shared_store = {}

    def fake_ctor(account_url=None, credential=None):
        assert account_url == "https://cceukbankdemo.blob.core.windows.net"
        return FakeBlobServiceClient(account_url=account_url, credential=credential, initial_store=shared_store)

    monkeypatch.setattr(pbs, "BlobServiceClient", fake_ctor)
    monkeypatch.setattr(pbs, "DefaultAzureCredential", lambda: "fake-credential")

    target_bytes = ub.bytes_from_gb(0.02) * len(ub.BUSINESS_DOMAINS)
    pbs.run("cceukbankdemo", target_bytes, seed=7)

    total_across_all = sum(sum(v.values()) for v in shared_store.values())
    assert total_across_all == target_bytes - (target_bytes % len(ub.BUSINESS_DOMAINS))

# CCE Azure Demo Data Generator — UK Bank Prospect

Populates an Azure environment with realistic-looking, UK-bank-themed, incompressible
test data for a Cohesity Cloud Edition (CCE) demo — the Azure-side counterpart to the
AWS environment already built for this POC.

- **10 VMs** (mix of `Standard_B2ms`/`Standard_B2s`), each with a 100GB data disk filled
  with UK-banking-themed files (~1TB total)
- **1TB of Blob storage**, spread across 10 containers named by business domain
  (mirrors the S3 bucket-per-domain layout from the AWS side)
- **1 Azure SQL Database** with ~300GB of data across a `customers`/`accounts`/`transactions`
  schema mirroring the AWS RDS side

Every population script is **idempotent and resumable**: each one checks what's already
there before writing anything, so re-running after an interrupted session tops up to the
target instead of duplicating or overshooting.

## File layout

```
provision/
  01_provision_azure_resources.sh   # Idempotent az CLI: RG, VNet, 10 VMs+disks, storage account, SQL DB
lib/
  ukbank_data.py                    # Shared: UK banking generators, fast random bytes, checkpoint store, batch sizing
vm_disks/
  generate_vm_data.py                # Runs ON each VM - fills its 100GB data disk
  orchestrate_vm_population.sh       # Runs generate_vm_data.py on all 10 VMs via `az vm run-command`
blob_storage/
  populate_blob_storage.py           # Fills 1TB across 10 domain-named containers
sql_database/
  schema.sql                         # customers / accounts / transactions
  populate_sql_database.py           # Populates ~300GB with memory-safe, resumable batches
tests/                                # 45 tests, all passing - see "What was actually tested" below
requirements.txt
```

## Design decisions worth knowing about

- **SQL schema mirrors the AWS RDS side**: `customers` / `accounts` / `transactions`, same
  narrative. If you'd rather branch out into more banking domains for this demo specifically,
  `populate_sql_database.py`'s row generators are written one-per-table, so adding a table is
  additive — extend `schema.sql` and add a `gen_<table>_row()` + `populate_<table>()` alongside
  the existing ones.
- **10 business domains = 10 VMs = 10 blob containers**: `core-banking`, `payments-hub`,
  `cards-processing`, `aml-compliance`, `risk-analytics`, `wealth-management`, `mobile-banking`,
  `branch-systems`, `data-warehouse`, `dr-replica`. Same names everywhere so the demo tells one
  consistent story across compute, blob, and SQL.
- **Fast incompressible random data**: benchmarked in-sandbox, numpy's PCG64 generator (`rng.bytes()`)
  beat `os.urandom` by ~50% (610 MB/s vs 400 MB/s on the test machine) with identical
  incompressibility (both compress to ~100.03% of original size under zlib). Not cryptographically
  secure, which doesn't matter for test data. `ukbank_data.random_bytes()` uses numpy when available,
  falls back to `os.urandom` otherwise.
- **IBANs are check-digit-valid**, not just the right shape — `gen_gb_iban()` implements the real
  ISO 7064 mod-97-10 algorithm and is tested against a published real-world example IBAN.
- **Batch sizing (the OOM-avoidance piece)**: `calculate_batch_size()` computes INSERT batch size
  from actual row size and a memory budget, rather than a flat default. The 4x overhead multiplier
  isn't a guess — it's calibrated against a `tracemalloc` measurement of this project's actual row
  generators (customers/accounts/transactions rows measured at 2.2x–2.9x raw field-byte counts;
  4x leaves headroom above that for driver/network layers tracemalloc can't see). See
  `tests/test_populate_sql_database.py` for the measurement and `ukbank_data.calculate_batch_size()`'s
  docstring for the reasoning.

## Prerequisites

```bash
az login
pip install -r requirements.txt
```

You'll also need the Microsoft ODBC Driver 18 for SQL Server for `populate_sql_database.py`
(pyodbc itself is pip-installable, but it delegates to a system driver — see below).

## Execution order

### 1. Provision the resources

```bash
cd provision
RESOURCE_GROUP=cce-ukbank-demo-rg LOCATION=uksouth ./01_provision_azure_resources.sh
```

Idempotent — safe to re-run if it's interrupted partway; anything already created is skipped.
**Note the SQL admin password it prints** — it's generated fresh each run and not stored anywhere
else by this script.

Takes a few minutes (VM creation is the slow part). **Pick `LOCATION` to match wherever you'll
run `populate_sql_database.py` from** — see "How long will this actually take" below for why
that matters more than it sounds like it should.

### 2. Fill the VM data disks (~1TB)

```bash
cd vm_disks
TARGET_GB=90 ./orchestrate_vm_population.sh cce-ukbank-demo-rg
```

Runs `generate_vm_data.py` on all 10 VMs via `az vm run-command invoke` — see "Cloud Shell
persistence" below for why this doesn't need a persistent SSH session or tmux at all. Add
`--wait` to run sequentially with output streamed back instead of all 10 in parallel.

### 3. Fill Blob Storage (1TB)

```bash
cd blob_storage
python3 populate_blob_storage.py --account-name <your-storage-account-name> --target-tb 1.0
```

This one **does** run from wherever you invoke it (not on a VM), for however long it takes to
push 1TB — see the persistence notes below for where to run it from.

### 4. Fill the SQL Database (~300GB)

Set up pyodbc's system driver first (one-time, on whichever machine will run this script):

```bash
curl -sSL -O https://packages.microsoft.com/config/ubuntu/22.04/packages-microsoft-prod.deb
sudo dpkg -i packages-microsoft-prod.deb
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev
```

**Cloud Shell has no `sudo`** (see below) — this step has to run on an actual VM, not Cloud Shell.

```bash
cd sql_database
python3 populate_sql_database.py \
  --conn-str "Driver={ODBC Driver 18 for SQL Server};Server=tcp:<sql-server-name>.database.windows.net,1433;Database=<db-name>;Uid=<sql-admin-user>;Pwd=<password>;Encrypt=yes;TrustServerCertificate=no;" \
  --target-gb 300 --customers 200000
```

#### How long will this actually take?

At the default row shape, **300GB works out to roughly 1.13 billion transaction rows**
(customers/accounts are negligible by comparison — a few hundred MB combined). The script uses
`fast_executemany` (pyodbc's array-binding mode — a well-known, large speedup over its default
row-by-row-emulated `executemany`), but real-world throughput for this pattern against Azure SQL
varies enormously by report — anywhere from roughly **2,000 to 20,000 rows/sec**, dominated by
network latency to the database far more than by CPU (row generation alone benchmarks at
30,000–100,000 rows/sec in this project, so it's not the bottleneck). At that range, 1.13B rows
is realistically **16 hours to 6+ days**. One Azure-specific report even hit outright timeouts at
batch sizes of only 10,000 over a non-co-located connection.

Two things you can do about that, both worth doing:

1. **Run the population script from a VM in the same Azure region as the SQL Database** — one of
   your 10 demo VMs works fine. Latency to Azure SQL is the dominant cost, and this is the single
   biggest lever on it. Running from your laptop or a different region will be dramatically slower.
2. **Do a small timed trial before committing to the full run**: `--target-gb 1 --customers 2000`
   finishes in seconds to a couple minutes and tells you your *actual* rows/sec on your actual
   region/tier/network, which you can then divide 1.13B by to get a real ETA instead of the wide
   estimate above.

If the real number comes back too slow for your timeline, `--transaction-metadata-bytes` is the
lever: it adds a realistic audit/fraud-scoring JSON field to each transaction row (device ID,
channel, session ID, fraud score — the kind of thing a real transaction pipeline logs), padded to
roughly the byte count you give it. Wider rows means fewer of them are needed to hit the same
`--target-gb`, roughly inversely — e.g. `--transaction-metadata-bytes 2000` cuts the row count
(and therefore the wall-clock time) by roughly 10x for the same 300GB target. Default is 0 (today's
~220-byte row, unchanged).

If you need it faster than that ceiling too, the next step up is Microsoft's own recommendation
for very large loads: `BULK INSERT` / the `bcp` utility (streams a CSV over the dedicated bulk-copy
protocol rather than issuing INSERT statements one batch at a time) — not implemented here, but
straightforward to adapt `populate_sql_database.py`'s row generators to write CSVs for if you get
there.

## Azure-specific notes (coming from AWS/EC2)

### Cloud Shell persistence — it's not what tmux gave you on EC2

Azure Cloud Shell mounts a small Azure Files share under your home directory (`clouddrive`) that
genuinely persists — files you save there are still there next time you open Cloud Shell. But
**the shell session itself is a container that gets torn down** after about 20 minutes of
inactivity, or whenever you close the tab. That's the opposite of an EC2 instance, where the OS
keeps running regardless of whether you're SSH'd in.

The practical consequence: `tmux`/`screen`/`nohup ... &` inside Cloud Shell do **not** protect a
long-running job the way they did on EC2 — when the container recycles, everything inside it
(panes, backgrounded processes, all of it) dies with it, not just your terminal connection to it.

**Also: no `sudo` in Cloud Shell.** Unlike an EC2 instance (or AWS Cloud9), you don't get root.
`apt-get install` won't work there — anything that needs it (like the ODBC driver setup above)
has to run on an actual VM.

### What actually gives you the EC2+tmux equivalent

- **For the VM data-disk step**: `az vm run-command invoke` (what `orchestrate_vm_population.sh`
  uses) executes your script via the Azure VM Guest Agent — a process on the VM's own OS,
  completely independent of your Cloud Shell/laptop session. It keeps running to completion even
  if Cloud Shell recycles or your laptop sleeps. This is the actual fire-and-forget primitive on
  Azure; reach for it before reaching for a persistent shell.
- **For the Blob and SQL population scripts** (which run from a client, not on a VM): the direct
  analog to your EC2+tmux workflow is to SSH into one of the demo VMs (or a small dedicated jump
  box) and run `tmux`/`screen`/`nohup` there — a real VM's OS persists exactly like EC2 did.
  Don't run these multi-hour jobs from Cloud Shell.
- If you want the job to survive a VM *reboot* too (not just your session), a systemd oneshot unit
  is the more Azure-native equivalent of an EC2 user-data/init script, but for a demo prep task
  tmux-on-a-VM is almost certainly enough.

## What was actually tested

This environment has no route to any Azure endpoint, so nothing here was run against real Azure
resources — exactly the constraint you flagged, and the reason every test below mocks the
Azure/DB layer rather than skipping verification:

- **`lib/ukbank_data.py`** (19 tests): every generator's output format, the IBAN checksum against
  a published real-world IBAN, incompressibility of the random-byte generator (checked via actual
  zlib compression ratio), and the checkpoint store's atomic-write/resume behavior.
- **`vm_disks/generate_vm_data.py`** (5 tests): fresh run reaches target exactly, re-running after
  "completion" doesn't duplicate, resuming after a simulated mid-run kill tops up correctly, and
  the manifest self-heals if a file goes missing underneath it. The orchestration script's
  per-VM remote-script generation was separately verified against a mocked `az` CLI — all 10
  generated scripts pass `bash -n`, and one was spot-checked byte-for-byte to confirm the embedded
  Python survived the heredoc substitution intact.
- **`blob_storage/populate_blob_storage.py`** (5 tests): against an in-memory fake of
  `BlobServiceClient`/`ContainerClient` — fresh population, resume-without-duplicating, no-op when
  already at target, and the streaming random-data generator's exact length + incompressibility.
- **`sql_database/populate_sql_database.py`** (16 tests): against a fake pyodbc connection —
  schema statement-splitting, batch-size sanity at both generous and tight memory budgets (the
  tight-budget case is the one that actually matters for OOM protection), fresh/resume/partial-
  interruption population for all three tables in FK order, the `--transaction-metadata-bytes`
  padding feature, and the IBAN checksum again on generated account rows.

Both bash scripts pass `shellcheck` clean at `info` severity (the strictest common level).

**Not tested, because it can't be from here**: actual throughput/latency against a real Azure SQL
Database or Storage account, actual VM disk mount/format behavior, and the `az` CLI calls in
`01_provision_azure_resources.sh` themselves (syntax-checked, but never run against a real
subscription). Worth a small trial run (`--target-gb 1`, `TARGET_GB=1`) before committing to the
full-scale versions, partly to catch anything environment-specific and partly to get a real
throughput number for the SQL runtime question above.

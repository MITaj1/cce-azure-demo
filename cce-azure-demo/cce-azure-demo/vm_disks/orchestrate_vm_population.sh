#!/usr/bin/env bash
#
# orchestrate_vm_population.sh
# Fires off generate_vm_data.py on all 10 demo VMs using `az vm run-command
# invoke`. This deliberately does NOT rely on a persistent SSH session or a
# Cloud Shell tmux pane: run-command executes the script as a background
# process ON the VM itself (via the VM agent), so it keeps running to
# completion even if Cloud Shell recycles or your laptop goes to sleep. See
# the "Long-running jobs from Cloud Shell" section of the README for why
# that distinction matters coming from an EC2/tmux workflow.
#
# Because generate_vm_data.py is itself idempotent (checkpointed via a
# manifest file on the data disk), this orchestration script is also safe
# to re-run in full: any VM that already finished will report "nothing to
# do" in seconds rather than redo work.
#
# Usage:
#   ./orchestrate_vm_population.sh <resource-group> [--wait]
#
# --wait polls each run-command to completion and prints its output
# sequentially. Without --wait, it fires all 10 asynchronously (background
# jobs) and prints a `az vm run-command invoke ... --async` style follow-up
# for you to check status commands.

set -euo pipefail

RESOURCE_GROUP="${1:-}"
WAIT_MODE=false
[[ "${2:-}" == "--wait" ]] && WAIT_MODE=true

if [[ -z "$RESOURCE_GROUP" ]]; then
    echo "Usage: $0 <resource-group> [--wait]" >&2
    exit 1
fi

# VM name <-> business domain mapping - matches lib/ukbank_data.py BUSINESS_DOMAINS
# and the container names created in blob_storage/populate_blob_storage.py, so
# the whole demo tells one consistent story across compute, blob, and SQL.
declare -A VM_DOMAINS=(
    ["vm-core-banking-01"]="core-banking"
    ["vm-payments-hub-01"]="payments-hub"
    ["vm-cards-processing-01"]="cards-processing"
    ["vm-aml-compliance-01"]="aml-compliance"
    ["vm-risk-analytics-01"]="risk-analytics"
    ["vm-wealth-management-01"]="wealth-management"
    ["vm-mobile-banking-01"]="mobile-banking"
    ["vm-branch-systems-01"]="branch-systems"
    ["vm-data-warehouse-01"]="data-warehouse"
    ["vm-dr-replica-01"]="dr-replica"
)

TARGET_GB="${TARGET_GB:-90}"          # per-VM target on the 100GB data disk
DATA_DISK_MOUNT="${DATA_DISK_MOUNT:-/mnt/datadisk}"
SCRIPT_LOCAL_PATH="$(dirname "$0")/generate_vm_data.py"
LIB_LOCAL_PATH="$(dirname "$0")/../lib/ukbank_data.py"

if [[ ! -f "$SCRIPT_LOCAL_PATH" || ! -f "$LIB_LOCAL_PATH" ]]; then
    echo "ERROR: expected generate_vm_data.py and ../lib/ukbank_data.py next to this script." >&2
    exit 1
fi

run_on_vm() {
    local vm_name="$1"
    local domain="$2"

    echo "=== $vm_name ($domain) ==="

    # run-command executes a bash script on the VM via the Azure VM agent.
    # We inline both files as a single heredoc-built script so there's no
    # dependency on the VM already having anything checked out - it only
    # needs Python 3, which we install first if missing.
    local remote_script
    remote_script=$(cat <<'REMOTE_EOF'
set -e
if ! command -v python3 >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-pip
fi
python3 -c "import numpy" 2>/dev/null || pip3 install --quiet numpy 2>/dev/null || true

mkdir -p /opt/cce-demo/lib /opt/cce-demo/vm_disks
cat > /opt/cce-demo/lib/ukbank_data.py <<'LIB_EOF'
__LIB_CONTENT__
LIB_EOF

cat > /opt/cce-demo/vm_disks/generate_vm_data.py <<'SCRIPT_EOF'
__SCRIPT_CONTENT__
SCRIPT_EOF

# Ensure the data disk is mounted before we try to fill it - if it's a
# freshly-attached raw disk, format+mount it (idempotent: skips if already
# mounted/formatted). Adjust /dev/sdc if your LUN differs.
if ! mountpoint -q __MOUNT__; then
    sudo mkdir -p __MOUNT__
    if ! blkid /dev/sdc1 >/dev/null 2>&1; then
        sudo parted /dev/sdc --script mklabel gpt mkpart primary ext4 0% 100%
        # parted returns before the kernel has necessarily registered the new
        # partition device node - mkfs.ext4 on /dev/sdc1 immediately after can
        # fail with "file does not exist" as a result. partprobe (or a settle
        # wait) forces that registration before proceeding.
        sudo partprobe /dev/sdc 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            [ -e /dev/sdc1 ] && break
            sleep 1
        done
        sudo mkfs.ext4 -F /dev/sdc1
    fi
    sudo mount /dev/sdc1 __MOUNT__
    sudo chmod 777 __MOUNT__
fi

python3 /opt/cce-demo/vm_disks/generate_vm_data.py --mount __MOUNT__ --domain __DOMAIN__ --target-gb __TARGET_GB__
REMOTE_EOF
)
    remote_script="${remote_script/__LIB_CONTENT__/$(cat "$LIB_LOCAL_PATH")}"
    remote_script="${remote_script/__SCRIPT_CONTENT__/$(cat "$SCRIPT_LOCAL_PATH")}"
    remote_script="${remote_script//__MOUNT__/$DATA_DISK_MOUNT}"
    remote_script="${remote_script//__DOMAIN__/$domain}"
    remote_script="${remote_script//__TARGET_GB__/$TARGET_GB}"

    echo "$remote_script" > "/tmp/run_${vm_name}.sh"

    if $WAIT_MODE; then
        az vm run-command invoke \
            --resource-group "$RESOURCE_GROUP" \
            --name "$vm_name" \
            --command-id RunShellScript \
            --scripts "@/tmp/run_${vm_name}.sh" \
            --query "value[0].message" -o tsv
    else
        # Fire-and-forget: az run-command itself blocks until the VM-side
        # script finishes, so to run all 10 VMs in parallel from your
        # workstation/Cloud Shell we background each `az` invocation.
        az vm run-command invoke \
            --resource-group "$RESOURCE_GROUP" \
            --name "$vm_name" \
            --command-id RunShellScript \
            --scripts "@/tmp/run_${vm_name}.sh" \
            > "/tmp/result_${vm_name}.log" 2>&1 &
        echo "  -> launched in background, PID $!, log: /tmp/result_${vm_name}.log"
    fi
}

for vm_name in "${!VM_DOMAINS[@]}"; do
    run_on_vm "$vm_name" "${VM_DOMAINS[$vm_name]}"
done

if ! $WAIT_MODE; then
    echo
    echo "All 10 run-command invocations launched in the background."
    echo "Each keeps running on its VM via the Azure VM agent even if this"
    echo "shell exits - check progress any time with:"
    echo "  jobs -l                      # from this same shell, while it's open"
    echo "  cat /tmp/result_<vm-name>.log"
    echo "Re-run this script at any time - completed VMs report 'nothing to do'"
    echo "in seconds rather than redoing work."
    wait
fi

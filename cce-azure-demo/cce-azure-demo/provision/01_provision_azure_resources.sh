#!/usr/bin/env bash
#
# 01_provision_azure_resources.sh
# Idempotent provisioning for the CCE Azure demo: resource group, network,
# 10 VMs (mix of Standard_B2ms/B2s) each with a 100GB data disk, a storage
# account, and an Azure SQL Database.
#
# Idempotent by design: every resource is created with a `show || create`
# check, so re-running this script after an interruption (or just to add a
# resource you edited in) skips anything already there rather than erroring
# or duplicating. Requires `az login` to already be done.
#
# Usage:
#   ./01_provision_azure_resources.sh
#
# Override any of the variables below via environment, e.g.:
#   RESOURCE_GROUP=cce-ukbank-demo LOCATION=uksouth ./01_provision_azure_resources.sh

set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-cce-ukbank-demo-rg}"
LOCATION="${LOCATION:-uksouth}"
VNET_NAME="${VNET_NAME:-cce-demo-vnet}"
SUBNET_NAME="${SUBNET_NAME:-cce-demo-subnet}"
NSG_NAME="${NSG_NAME:-cce-demo-nsg}"
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-cceukbankdemo$(az account show --query id -o tsv 2>/dev/null | tail -c 6)}"
SQL_SERVER_NAME="${SQL_SERVER_NAME:-cce-ukbank-sql-$(az account show --query id -o tsv 2>/dev/null | tail -c 6)}"
SQL_DB_NAME="${SQL_DB_NAME:-cceukbankdb}"
SQL_ADMIN_USER="${SQL_ADMIN_USER:-cceadmin}"
DATA_DISK_GB="${DATA_DISK_GB:-100}"
ADMIN_USERNAME="${ADMIN_USERNAME:-cceadmin}"

# VM name -> (business domain, size). Domain must match lib/ukbank_data.py's
# BUSINESS_DOMAINS and the container names populate_blob_storage.py creates,
# so compute, blob, and SQL all tell the same demo story. Split roughly
# evenly across the two requested SKUs.
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
declare -A VM_SIZES=(
    ["vm-core-banking-01"]="Standard_B2ms"
    ["vm-payments-hub-01"]="Standard_B2ms"
    ["vm-cards-processing-01"]="Standard_D2s_v3"
    ["vm-aml-compliance-01"]="Standard_D2s_v3"
    ["vm-data-warehouse-01"]="Standard_D2s_v3"
    ["vm-risk-analytics-01"]="Standard_D2s_v3"
    ["vm-wealth-management-01"]="Standard_D2s_v3"
    ["vm-mobile-banking-01"]="Standard_D2s_v3"
    ["vm-branch-systems-01"]="Standard_B2s"
    ["vm-dr-replica-01"]="Standard_D2s_v3"
)
log() { echo "[$(date +%H:%M:%S)] $*"; }

# --- Resource group -----------------------------------------------------
if az group show --name "$RESOURCE_GROUP" &>/dev/null; then
    log "Resource group $RESOURCE_GROUP already exists, skipping"
else
    log "Creating resource group $RESOURCE_GROUP in $LOCATION"
    az group create --name "$RESOURCE_GROUP" --location "$LOCATION" -o none
fi

# --- Network: VNet, subnet, NSG -----------------------------------------
if az network vnet show --resource-group "$RESOURCE_GROUP" --name "$VNET_NAME" &>/dev/null; then
    log "VNet $VNET_NAME already exists, skipping"
else
    log "Creating VNet $VNET_NAME / subnet $SUBNET_NAME"
    az network vnet create \
        --resource-group "$RESOURCE_GROUP" --name "$VNET_NAME" \
        --address-prefix 10.20.0.0/16 \
        --subnet-name "$SUBNET_NAME" --subnet-prefix 10.20.1.0/24 -o none
fi

if az network nsg show --resource-group "$RESOURCE_GROUP" --name "$NSG_NAME" &>/dev/null; then
    log "NSG $NSG_NAME already exists, skipping"
else
    log "Creating NSG $NSG_NAME (allowing inbound SSH - tighten the source range for anything beyond a demo)"
    az network nsg create --resource-group "$RESOURCE_GROUP" --name "$NSG_NAME" -o none
    az network nsg rule create \
        --resource-group "$RESOURCE_GROUP" --nsg-name "$NSG_NAME" \
        --name allow-ssh --priority 1000 --access Allow --protocol Tcp \
        --destination-port-ranges 22 -o none
fi

# --- VMs + data disks -----------------------------------------------------
for vm_name in "${!VM_DOMAINS[@]}"; do
    domain="${VM_DOMAINS[$vm_name]}"
    size="${VM_SIZES[$vm_name]}"

    if az vm show --resource-group "$RESOURCE_GROUP" --name "$vm_name" &>/dev/null; then
        log "$vm_name already exists, skipping ($domain, $size)"
        continue
    fi

    log "Creating $vm_name ($domain, $size, +${DATA_DISK_GB}GB data disk, no public IP - private-VNet-only)"
    az vm create \
        --resource-group "$RESOURCE_GROUP" \
        --name "$vm_name" \
        --image Ubuntu2204 \
        --size "$size" \
        --vnet-name "$VNET_NAME" --subnet "$SUBNET_NAME" \
        --nsg "$NSG_NAME" \
        --public-ip-address "" \
        --admin-username "$ADMIN_USERNAME" \
        --generate-ssh-keys \
        --data-disk-sizes-gb "$DATA_DISK_GB" \
        --storage-sku StandardSSD_LRS \
        --tags "domain=$domain" "project=cce-ukbank-demo" \
        -o none
done

# --- Storage account -------------------------------------------------------
if az storage account show --resource-group "$RESOURCE_GROUP" --name "$STORAGE_ACCOUNT" &>/dev/null; then
    log "Storage account $STORAGE_ACCOUNT already exists, skipping"
else
    log "Creating storage account $STORAGE_ACCOUNT"
    az storage account create \
        --resource-group "$RESOURCE_GROUP" --name "$STORAGE_ACCOUNT" \
        --location "$LOCATION" --sku Standard_LRS --kind StorageV2 -o none
fi
# Containers themselves are created idempotently by populate_blob_storage.py
# (ensure_container()) - no need to duplicate that here.

# --- Azure SQL: logical server + firewall + database -----------------------
if az sql server show --resource-group "$RESOURCE_GROUP" --name "$SQL_SERVER_NAME" &>/dev/null; then
    log "SQL server $SQL_SERVER_NAME already exists, skipping"
else
    log "Creating SQL server $SQL_SERVER_NAME"
    SQL_ADMIN_PASSWORD="$(openssl rand -base64 24)"
    az sql server create \
        --resource-group "$RESOURCE_GROUP" --name "$SQL_SERVER_NAME" \
        --location "$LOCATION" \
        --admin-user "$SQL_ADMIN_USER" --admin-password "$SQL_ADMIN_PASSWORD" \
        -o none
    log "SQL admin password (SAVE THIS - not stored anywhere else by this script):"
    echo "  $SQL_ADMIN_PASSWORD"

    log "Allowing Azure services to reach $SQL_SERVER_NAME (needed for run-command/Cloud Shell access)"
    az sql server firewall-rule create \
        --resource-group "$RESOURCE_GROUP" --server "$SQL_SERVER_NAME" \
        --name AllowAzureServices --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 -o none

    MY_IP="$(curl -s https://api.ipify.org || true)"
    if [[ -n "$MY_IP" ]]; then
        log "Allowing your current public IP ($MY_IP) through the firewall"
        az sql server firewall-rule create \
            --resource-group "$RESOURCE_GROUP" --server "$SQL_SERVER_NAME" \
            --name AllowMyCurrentIP --start-ip-address "$MY_IP" --end-ip-address "$MY_IP" -o none
    fi
fi

if az sql db show --resource-group "$RESOURCE_GROUP" --server "$SQL_SERVER_NAME" --name "$SQL_DB_NAME" &>/dev/null; then
    log "SQL database $SQL_DB_NAME already exists, skipping"
else
    log "Creating SQL database $SQL_DB_NAME (General Purpose Gen5, 2 vCore, 400GB max - headroom over the 300GB target)"
    az sql db create \
        --resource-group "$RESOURCE_GROUP" --server "$SQL_SERVER_NAME" --name "$SQL_DB_NAME" \
        --edition GeneralPurpose --family Gen5 --capacity 2 --max-size 400GB \
        -o none
fi

log "Done. Resource group: $RESOURCE_GROUP"
log "  VMs:            10 (see 'az vm list -g $RESOURCE_GROUP -o table')"
log "  Storage account: $STORAGE_ACCOUNT"
log "  SQL server/db:   $SQL_SERVER_NAME / $SQL_DB_NAME"

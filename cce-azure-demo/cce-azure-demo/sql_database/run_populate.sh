#!/bin/bash
#
# run_populate.sh
# Wrapper for populate_sql_database.py so you don't have to retype the full
# --conn-str command by hand every time.
#
# The password is deliberately NOT in this file - this repo is public, and
# a real (even demo-environment) SQL admin password committed to a public
# GitHub repo is discoverable within seconds by bots that scan for exactly
# this pattern. Instead, set it once as an environment variable before
# running this script - that command is typed directly on the VM and never
# touches git:
#
#   export CCE_SQL_PASSWORD='MySecure!Pass123'
#
# Use SINGLE quotes for that export - bash's history expansion treats `!`
# specially even inside double quotes, but single-quoted strings are fully
# literal. Single quotes avoid needing to backslash-escape anything.
#
# Usage:
#   ./run_populate.sh <target-gb> <customers> <metadata-bytes>
#   ./run_populate.sh                    # defaults: 1 GB, 2000 customers, no metadata
#   ./run_populate.sh 300 200000          # the real run, narrow rows (~1.1B rows, slow)
#   ./run_populate.sh 300 200000 2000     # wide rows (~110M rows, ~10x faster)

set -e

if [ -z "$CCE_SQL_PASSWORD" ]; then
    echo "ERROR: CCE_SQL_PASSWORD is not set. Run this first (single quotes, see comment above):"
    echo "  export CCE_SQL_PASSWORD='<your password>'"
    exit 1
fi

TARGET_GB="${1:-1}"
CUSTOMERS="${2:-2000}"
METADATA_BYTES="${3:-0}"

python3 populate_sql_database.py \
  --conn-str "Driver={ODBC Driver 18 for SQL Server};Server=tcp:bank-services-server.database.windows.net,1433;Database=cceukbankdb;Uid=dbadmin;Pwd=${CCE_SQL_PASSWORD};Encrypt=yes;TrustServerCertificate=no;" \
  --target-gb "$TARGET_GB" --customers "$CUSTOMERS" \
  --transaction-metadata-bytes "$METADATA_BYTES"

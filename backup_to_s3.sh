#!/usr/bin/env bash
set -euo pipefail

# Configuration
PROJECT_DIR="/var/www/url-redirect"
DB_SRC="${PROJECT_DIR}/data/redirects.db"
BACKUP_DIR="${PROJECT_DIR}/backups"
S3_BUCKET="s3://luap-prod/url-redirect"
TIMESTAMP=$(date -u +"%Y%m%d_%H%M%SZ")
BACKUP_FILE="${BACKUP_DIR}/redirects_${TIMESTAMP}.db.gz"

# Ensure local temporary backup dir exists
mkdir -p "${BACKUP_DIR}"

# 1. Take a hot, consistent snapshot of the SQLite database
# The .backup command safely handles any active writes
sqlite3 "${DB_SRC}" ".backup '${BACKUP_DIR}/temp_${TIMESTAMP}.db'"

# 2. Compress the snapshot to save bandwidth and S3 storage
gzip -c "${BACKUP_DIR}/temp_${TIMESTAMP}.db" > "${BACKUP_FILE}"
rm -f "${BACKUP_DIR}/temp_${TIMESTAMP}.db"

# 3. Upload to AWS S3 (uses credentials configured in ~/.aws or IAM)
aws s3 cp "${BACKUP_FILE}" "${S3_BUCKET}/$(basename "${BACKUP_FILE}")"

# 4. Prune local backups older than 7 days
find "${BACKUP_DIR}" -name "redirects_*.db.gz" -type f -mtime +7 -delete

echo "[$(date -u)] Backup ${BACKUP_FILE} uploaded successfully to ${S3_BUCKET}."
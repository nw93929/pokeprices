#!/usr/bin/env bash
# Build a Lambda layer with matplotlib (and its deps numpy/pillow) and publish it.
#
# Why this exists: matplotlib + numpy + pillow are too big to bundle into a
# Chalice deployment package. We put them in a layer published from S3
# (raises the size cap from 50 MB to 250 MB unzipped) and reference that
# layer in .chalice/config.json.
#
# Usage:
#   ./scripts/build_matplotlib_layer.sh
#
# Environment overrides (defaults shown):
#   BUCKET=p3-pokemon-cardprices       S3 bucket to stage the zip
#   REGION=us-east-1                   AWS region
#   LAYER_NAME=card-prices-matplotlib  Lambda layer name
#   PYTHON_VER=3.12                    Lambda Python runtime
#
# Requires: aws CLI configured, zip, pip. Run from the project root.

set -euo pipefail

BUCKET="${BUCKET:-p3-pokemon-cardprices}"
REGION="${REGION:-us-east-1}"
LAYER_NAME="${LAYER_NAME:-card-prices-matplotlib}"
PYTHON_VER="${PYTHON_VER:-3.12}"
PLATFORM="manylinux2014_x86_64"

log() { echo "[build-layer] $*"; }

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

mkdir -p "$WORK_DIR/python"

log "Installing matplotlib for Lambda Python ${PYTHON_VER} (${PLATFORM})"
# --platform + --only-binary forces manylinux wheels so the C extensions match
# Lambda's runtime, even though we're building on WSL/Ubuntu.
pip install \
  --target "$WORK_DIR/python" \
  --platform "$PLATFORM" \
  --python-version "$PYTHON_VER" \
  --implementation cp \
  --only-binary=:all: \
  --upgrade \
  --no-cache-dir \
  matplotlib

log "Stripping bloat (tests, caches, sample data, debug symbols)"
cd "$WORK_DIR/python"
find . -type d -name "__pycache__" -prune -exec rm -rf {} +
find . -type d -name "tests" -prune -exec rm -rf {} +
find . -type d -name "test" -prune -exec rm -rf {} +
find . -name "*.pyc" -delete
find . -name "*.pyi" -delete
rm -rf matplotlib/mpl-data/sample_data 2>/dev/null || true
# Strip debug symbols where strip is available; harmless if it fails.
find . -name "*.so" -exec strip --strip-unneeded {} \; 2>/dev/null || true

cd "$WORK_DIR"
log "Unzipped layer size:"
du -sh python/

log "Zipping layer"
zip -qr layer.zip python/
log "Zipped layer size:"
ls -lh layer.zip

S3_KEY="layers/matplotlib-py${PYTHON_VER}.zip"
log "Uploading to s3://${BUCKET}/${S3_KEY}"
aws s3 cp layer.zip "s3://${BUCKET}/${S3_KEY}" --region "$REGION"

log "Publishing layer ${LAYER_NAME}"
ARN=$(aws lambda publish-layer-version \
  --layer-name "$LAYER_NAME" \
  --description "matplotlib + numpy + pillow for card-prices" \
  --content "S3Bucket=${BUCKET},S3Key=${S3_KEY}" \
  --compatible-runtimes "python${PYTHON_VER}" \
  --compatible-architectures "x86_64" \
  --region "$REGION" \
  --query 'LayerVersionArn' --output text)

echo
echo "==================================================================="
echo "Layer ARN:"
echo "  $ARN"
echo "==================================================================="
echo
echo "Next steps:"
echo "  1. Paste that ARN into .chalice/config.json under 'layers'."
echo "  2. Run: chalice deploy"

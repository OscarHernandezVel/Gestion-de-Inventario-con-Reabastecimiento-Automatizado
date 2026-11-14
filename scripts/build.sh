#!/usr/bin/env bash
# Empaqueta todas las Lambdas en build/lambda.zip (Python 3.12, arm64).
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf build && mkdir -p build/pkg
pip install --quiet --target build/pkg --platform manylinux2014_aarch64 --python-version 3.12 \
  --only-binary=:all: -r backend/requirements.txt
cp -r backend/inventario build/pkg/
find build/pkg/inventario -name __pycache__ -prune -exec rm -rf {} +
(cd build/pkg && python -c "import shutil; shutil.make_archive('../lambda', 'zip', '.')")
echo "build/lambda.zip listo"

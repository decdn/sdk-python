#!/usr/bin/env bash
# Builds the sdist and wheel, validates the metadata, and smoke-tests the wheel in a fresh venv.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

uv build --out-dir "$tmp/dist" >/dev/null 2>&1
echo "ok: built sdist and wheel"

uv run twine check "$tmp"/dist/* >/dev/null
echo "ok: twine check"

uv venv "$tmp/venv" >/dev/null 2>&1
uv pip install --python "$tmp/venv/bin/python" "$tmp"/dist/*.whl >/dev/null 2>&1
echo "ok: installed the wheel into a fresh venv"

version="$("$tmp/venv/bin/python" -c "import decdn, decdn.local; decdn.Client; print(decdn.__version__)")"
echo "ok: imported decdn $version"

if ! python3 -m zipfile -l "$tmp"/dist/*.whl | grep -q "decdn/py.typed"; then
  echo "error: decdn/py.typed is missing from the wheel" >&2
  exit 1
fi
echo "ok: py.typed is in the wheel"

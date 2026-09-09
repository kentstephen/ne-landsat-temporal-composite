#!/bin/zsh
cd "$(dirname "$0")/.."
while kill -0 $(cat data/export.pid) 2>/dev/null; do sleep 5; done
echo "=== export done $(date)"; tail -2 data/logs/export_v2.log
echo "=== export verify $(date)"
uv run python src/landsat_mosaic/export_zarr.py --verify --ref v2 --years 2003-2012 --sample 20 > data/logs/export_v2_verify.log 2>&1; echo "verify exit $?"; tail -3 data/logs/export_v2_verify.log
echo "=== pyramid build $(date)"
uv run python src/landsat_mosaic/pyramid.py --years 2003-2012 --force --workers 6 > data/logs/pyramid_build_v2.log 2>&1; echo "build exit $?"; tail -3 data/logs/pyramid_build_v2.log
echo "=== finalize $(date)"
uv run python src/landsat_mosaic/pyramid.py --finalize > data/logs/pyramid_finalize_v2.log 2>&1; echo "finalize exit $?"; tail -3 data/logs/pyramid_finalize_v2.log
echo "=== verify $(date)"
uv run python src/landsat_mosaic/pyramid.py --verify --workers 6 > data/logs/pyramid_verify_v2.log 2>&1; echo "verify exit $?"; tail -3 data/logs/pyramid_verify_v2.log
echo "=== chain done $(date)"

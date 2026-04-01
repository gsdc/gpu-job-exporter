#!/bin/bash
# build_rpm.sh — Vendor Python deps and build the gpu-job-exporter RPM.
# Usage: bash build_rpm.sh
# Requirements: python3, pip3, rpmbuild  (dnf install rpm-build python3-pip)
set -euo pipefail

NAME="gpu-job-exporter"
VERSION="1.0.0"
SRCDIR="${NAME}-${VERSION}"

# ── 1. Sanity checks ─────────────────────────────────────────────────────────
for cmd in python3 pip3 rpmbuild; do
    command -v "$cmd" &>/dev/null || { echo "ERROR: '$cmd' not found."; exit 1; }
done

# ── 2. Assemble source tree ───────────────────────────────────────────────────
echo ">>> Building source tree: ${SRCDIR}/"
rm -rf "${SRCDIR}"
mkdir -p "${SRCDIR}/lib"

cp gpu_job_exporter.py  "${SRCDIR}/"
cp "${NAME}.service"    "${SRCDIR}/"

# ── 3. Vendor Python dependencies into lib/ ───────────────────────────────────
echo ">>> Installing Python deps into ${SRCDIR}/lib/ ..."
pip3 install \
    --target="${SRCDIR}/lib" \
    --quiet \
    --no-compile \
    "prometheus_client>=0.20.0" \
    "psutil>=5.9.0"

# Strip __pycache__ and test directories to keep the RPM lean
find "${SRCDIR}/lib" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "${SRCDIR}/lib" -type d -name "tests"       -exec rm -rf {} + 2>/dev/null || true
find "${SRCDIR}/lib" -name "*.pyc"               -delete 2>/dev/null           || true

echo ">>> Vendored lib/ size: $(du -sh "${SRCDIR}/lib" | cut -f1)"

# ── 4. Create source tarball ──────────────────────────────────────────────────
TARBALL="${SRCDIR}.tar.gz"
echo ">>> Creating tarball: ${TARBALL}"
tar czf "${TARBALL}" "${SRCDIR}"
rm -rf "${SRCDIR}"

# ── 5. Set up rpmbuild tree ───────────────────────────────────────────────────
mkdir -p ~/rpmbuild/{SPECS,SOURCES,BUILD,RPMS,SRPMS}
cp "${TARBALL}"   ~/rpmbuild/SOURCES/
cp "${NAME}.spec" ~/rpmbuild/SPECS/

# ── 6. Build RPM ─────────────────────────────────────────────────────────────
echo ">>> Running rpmbuild ..."
rpmbuild -ba ~/rpmbuild/SPECS/"${NAME}".spec

# ── 7. Report output ─────────────────────────────────────────────────────────
echo ""
echo "=== Build complete ==="
find ~/rpmbuild/RPMS ~/rpmbuild/SRPMS -name "${NAME}*.rpm" | sort | while read -r f; do
    echo "  $f"
done
echo ""
echo "Install with:"
echo "  sudo rpm  -ivh ~/rpmbuild/RPMS/x86_64/${NAME}-${VERSION}-1.*.rpm"
echo "  sudo dnf install ~/rpmbuild/RPMS/x86_64/${NAME}-${VERSION}-1.*.rpm"

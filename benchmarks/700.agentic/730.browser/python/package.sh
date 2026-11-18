#!/bin/bash
# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.

# Bundles headless Chromium into the code package, so the function does not depend on
# system packages of the runtime image (the SeBS images, like AWS Lambda, have none of them).
# Runs in the SeBS build container (Debian, non-root user) from the package root, after pip.
#
# Result (moved later into function/ next to function.py):
#   browser/chromium_headless_shell-*/   Chromium build matching the pinned Playwright version
#   browser/lib/                         shared libraries Chromium needs that the image lacks
#   browser/fonts/                       fonts, so text renders identically everywhere

set -euo pipefail

PACKAGES_DIR=$1
BROWSER_DIR=$(pwd)/browser
LIB_DIR=${BROWSER_DIR}/lib
FONT_DIR=${BROWSER_DIR}/fonts
WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT

# Playwright downloads the browser for the machine it runs on.
MACHINE=$(uname -m)
if [[ "${TARGET_ARCHITECTURE:-}" == "arm64" && "${MACHINE}" != "aarch64" ]] ||
   [[ "${TARGET_ARCHITECTURE:-}" == "x64" && "${MACHINE}" != "x86_64" ]]; then
  echo "ERROR: 730.browser cannot be cross-built (target ${TARGET_ARCHITECTURE}, build machine ${MACHINE})" >&2
  exit 1
fi

# 1. Chromium headless shell: the headless-only build, smaller than full Chromium.
PYTHONPATH=${PACKAGES_DIR} PLAYWRIGHT_BROWSERS_PATH=${BROWSER_DIR} \
  python3 -m playwright install --only-shell chromium
rm -rf "${BROWSER_DIR}"/ffmpeg-*
CHROME=$(find "${BROWSER_DIR}" -name chrome-headless-shell -type f)

# 2. System libraries. apt-get needs root to install, but not to download and unpack,
# so we use a private package index in ${WORK}.
APT_OPTS="-o Dir::State::Lists=${WORK}/apt/lists -o Dir::Cache=${WORK}/apt/cache -o Debug::NoLocking=1"
mkdir -p "${WORK}"/apt/lists/partial "${WORK}"/apt/cache/archives/partial "${WORK}"/debs "${WORK}"/root
apt-get ${APT_OPTS} update -qq

# Chromium's dependencies on Debian 13, as listed by Playwright (install-deps), plus fonts.
PACKAGES="libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2
  libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0
  libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2
  libfontconfig1 libfreetype6 fonts-liberation"
# Download everything these depend on and the image lacks; unpacking is cheap, and step 3
# keeps only what Chromium loads. Alternatives such as GPU drivers or extra fonts are skipped.
SKIP='^(libllvm|mesa-|libdrm-(amdgpu|radeon|nouveau|intel)|libz3|libsensors|cdebconf|fonts-(noto|urw|texgyre|croscore|freefont|dejavu))'
ALL=$(apt-cache ${APT_OPTS} depends --recurse --no-recommends --no-suggests --no-conflicts \
        --no-breaks --no-replaces --no-enhances ${PACKAGES} | grep -v '^[ <]' | sort -u | grep -Ev "${SKIP}")
NEEDED=""
for pkg in ${ALL}; do
  dpkg -s "${pkg}" >/dev/null 2>&1 || NEEDED="${NEEDED} ${pkg}"
done
(cd "${WORK}/debs" && apt-get ${APT_OPTS} download ${NEEDED} >/dev/null)
for deb in "${WORK}"/debs/*.deb; do
  dpkg-deb -x "${deb}" "${WORK}/root"
done
SYS_LIBS=${WORK}/root/usr/lib/${MACHINE}-linux-gnu

# 3. Copy only libraries Chromium and Playwright's Node.js driver link against, plus those
# Chromium opens at runtime (dlopen), which ldd cannot see: fontconfig for text, NSS modules
# for its crypto initialization. Libraries resolved from the build image are copied too, since
# the run image is slimmer (e.g., no libatomic); only glibc itself is shared with every image.
mkdir -p "${LIB_DIR}" "${FONT_DIR}"
GLIBC='/(libc|libm|libdl|libpthread|librt|libresolv|libutil|ld-linux[^/]*)\.so'
SEEDS="${CHROME} ${PACKAGES_DIR}/playwright/driver/node ${SYS_LIBS}/libfontconfig.so.1"
for nss_module in libsoftokn3.so libfreebl3.so libfreeblpriv3.so; do
  if [[ -e "${SYS_LIBS}/${nss_module}" ]]; then
    SEEDS="${SEEDS} ${SYS_LIBS}/${nss_module}"
    cp -L "${SYS_LIBS}/${nss_module}" "${LIB_DIR}/"
    cp -L "${SYS_LIBS}/${nss_module%.so}.chk" "${LIB_DIR}/" 2>/dev/null || true
  fi
done
cp -L "${SYS_LIBS}/libfontconfig.so.1" "${LIB_DIR}/"
for seed in ${SEEDS}; do
  LD_LIBRARY_PATH=${SYS_LIBS} ldd "${seed}" | awk '/=>/ {print $3}' | { grep -Ev "${GLIBC}" || true; } |
    while read -r lib; do cp -L "${lib}" "${LIB_DIR}/"; done
done

cp "${WORK}"/root/usr/share/fonts/truetype/liberation/*.ttf "${FONT_DIR}/"

# Fail loudly at build time instead of at the first invocation.
for seed in "${CHROME}" "${PACKAGES_DIR}/playwright/driver/node" "${LIB_DIR}"/*.so*; do
  if LD_LIBRARY_PATH=${LIB_DIR} ldd "${seed}" 2>/dev/null | grep -q "not found"; then
    echo "ERROR: unresolved libraries for ${seed}:" >&2
    LD_LIBRARY_PATH=${LIB_DIR} ldd "${seed}" | grep "not found" >&2
    exit 1
  fi
done

echo "Browser $(du -sh "${BROWSER_DIR}" | cut -f1): Chromium $(du -sh "$(dirname "${CHROME}")" | cut -f1), libraries $(du -sh "${LIB_DIR}" | cut -f1), fonts $(du -sh "${FONT_DIR}" | cut -f1)"

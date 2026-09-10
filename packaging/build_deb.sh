#!/usr/bin/env bash
# Build the Ubuntu package: two PyInstaller bundles, wrapped in a .deb.
#
#   ./packaging/build_deb.sh              # build into installers/linux/<version>/
#   ./packaging/build_deb.sh --keep-venv  # reuse the build venv (much faster)
#
# Needs on the build machine: python3 with venv, dpkg-deb, fakeroot. Nothing is
# needed on the machine that installs the result.
#
# The version comes from pyproject.toml, which says it is the only copy - so it
# is read from there and written into the bundle as VERSION, where the frozen
# session_launcher.version() reads it back (importlib.metadata cannot answer
# inside a freeze).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build"
VENV="$BUILD/venv"
STAGE="$BUILD/deb"
ICONS="$BUILD/icons"
PACKAGE=qavector
APP_DIR="/opt/$PACKAGE"
KEEP_VENV=0
[ "${1:-}" = --keep-venv ] && KEEP_VENV=1

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

for tool in python3 dpkg-deb fakeroot; do
  command -v "$tool" >/dev/null || die "$tool is not installed"
done

VERSION="$(python3 - "$ROOT/pyproject.toml" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
sys.exit("no version in pyproject.toml") if not match else print(match.group(1))
PY
)"
ARCH="$(dpkg --print-architecture)"
OUT="$ROOT/installers/linux/$VERSION"
MAINTAINER="${DEB_MAINTAINER:-Yurii Potapov <potapovyura@gmail.com>}"

say "QAVector $VERSION ($ARCH)"

# -- 1. build environment -----------------------------------------------------
if [ "$KEEP_VENV" = 0 ] || [ ! -x "$VENV/bin/python" ]; then
  say "Creating the build environment"
  rm -rf "$VENV"
  python3 -m venv "$VENV"
  # Playwright ships a Chromium downloader that runs on install. We never use a
  # downloaded browser - the adapter only ever connect_over_cdp's to the Chrome
  # already on the machine - so skip it and save ~400 MB.
  PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 "$VENV/bin/pip" install -q --upgrade \
      pip wheel
  PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 "$VENV/bin/pip" install -q \
      -r "$ROOT/requirements.txt" -r "$ROOT/gui/requirements.txt" \
      "pyinstaller>=6.6" pyinstaller-hooks-contrib
fi

# -- 2. version stamp ---------------------------------------------------------
mkdir -p "$BUILD"
printf '%s\n' "$VERSION" > "$BUILD/VERSION"

# -- 3. freeze ----------------------------------------------------------------
rm -rf "$BUILD/dist" "$BUILD/pyi"
for spec in core gui; do
  say "Freezing $spec"
  ( cd "$ROOT" && "$VENV/bin/pyinstaller" --noconfirm --clean \
      --distpath "$BUILD/dist" --workpath "$BUILD/pyi" \
      "packaging/pyinstaller/$spec.spec" )
done

CORE_BIN="$BUILD/dist/core/qavector-core"
GUI_BIN="$BUILD/dist/gui/qavector-gui"
[ -x "$CORE_BIN" ] || die "the core bundle was not produced"
[ -x "$GUI_BIN" ] || die "the GUI bundle was not produced"

# PyInstaller copies datas without their permission bits, and playwright's driver
# is an executable it has to be able to run. Without this, connect_over_cdp fails
# with a permission error the first time a flow is executed - long after build.
NODE="$BUILD/dist/core/_internal/playwright/driver/node"
[ -f "$NODE" ] || die "playwright's node driver is missing from the core bundle"
chmod +x "$NODE"

# -- 4. sanity-check the frozen core ------------------------------------------
# Cheap, and it catches the two failures that are otherwise only visible to the
# person who installs the package: a lazy import the spec did not list, and a
# resource that did not make it into the bundle.
say "Checking the frozen core"
CMS_HOME="$BUILD/smoke" "$CORE_BIN" --version
CMS_HOME="$BUILD/smoke" "$CORE_BIN" --describe > "$BUILD/describe.json"
python3 "$ROOT/packaging/check_frozen.py" "$BUILD/describe.json" "$VERSION"

# -- 5. icons -----------------------------------------------------------------
say "Rendering icons"
rm -rf "$ICONS" && mkdir -p "$ICONS"
( cd "$ROOT/gui" && QT_QPA_PLATFORM=offscreen "$VENV/bin/python" -m cms_gui.icon "$ICONS" >/dev/null )

# -- 6. stage the package tree ------------------------------------------------
say "Staging the package"
rm -rf "$STAGE"
install -d "$STAGE/DEBIAN" "$STAGE$APP_DIR" "$STAGE/usr/bin" \
           "$STAGE/usr/share/applications" "$STAGE/usr/share/doc/$PACKAGE"

cp -a "$BUILD/dist/core" "$STAGE$APP_DIR/core"
cp -a "$BUILD/dist/gui" "$STAGE$APP_DIR/gui"
install -m 644 "$BUILD/VERSION" "$STAGE$APP_DIR/VERSION"

install -m 755 "$ROOT/packaging/linux/wrapper-core.sh" "$STAGE/usr/bin/$PACKAGE"
install -m 755 "$ROOT/packaging/linux/wrapper-gui.sh" "$STAGE/usr/bin/$PACKAGE-gui"
install -m 644 "$ROOT/packaging/linux/qavector-gui.desktop" \
        "$STAGE/usr/share/applications/$PACKAGE.desktop"
install -m 644 "$ROOT/packaging/linux/copyright" "$STAGE/usr/share/doc/$PACKAGE/copyright"

for size in 16 24 32 48 64 128 256; do
  icon="$ICONS/icon-$size.png"
  [ -f "$icon" ] || continue
  install -d "$STAGE/usr/share/icons/hicolor/${size}x${size}/apps"
  install -m 644 "$icon" \
      "$STAGE/usr/share/icons/hicolor/${size}x${size}/apps/$PACKAGE.png"
done

# Debian wants the changelog gzipped, and lintian complains if it is not.
gzip -9nc "$ROOT/CHANGELOG.md" > "$STAGE/usr/share/doc/$PACKAGE/changelog.gz"

install -m 755 "$ROOT/packaging/linux/postinst" "$STAGE/DEBIAN/postinst"
install -m 755 "$ROOT/packaging/linux/postrm" "$STAGE/DEBIAN/postrm"

INSTALLED_SIZE="$(du -sk "$STAGE" | cut -f1)"
sed -e "s|@VERSION@|$VERSION|" \
    -e "s|@MAINTAINER@|$MAINTAINER|" \
    -e "s|@INSTALLED_SIZE@|$INSTALLED_SIZE|" \
    -e "s|^Architecture: .*|Architecture: $ARCH|" \
    "$ROOT/packaging/linux/control.in" > "$STAGE/DEBIAN/control"

# -- 7. build -----------------------------------------------------------------
say "Building the .deb"
mkdir -p "$OUT"
DEB="$OUT/${PACKAGE}_${VERSION}_${ARCH}.deb"
# fakeroot so everything inside is owned by root:root without needing to be root.
fakeroot dpkg-deb --build --root-owner-group "$STAGE" "$DEB" >/dev/null

# The short, version-less name is the one the install instructions use.
cp -f "$DEB" "$OUT/qavector_${ARCH}.deb"
( cd "$OUT" && sha256sum ./*.deb > SHA256SUMS )

command -v lintian >/dev/null && lintian --no-tag-display-limit "$DEB" || true

say "Done"
printf '  %s\n  %s\n  %s\n\n' "$DEB" "$OUT/qavector_${ARCH}.deb" "$OUT/SHA256SUMS"
printf 'Install with:\n  sudo apt install %s\n' "$OUT/qavector_${ARCH}.deb"
printf '  (apt, not dpkg -i, so the Qt libraries it depends on are pulled in too)\n'

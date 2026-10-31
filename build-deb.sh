#!/bin/sh
# Build quickunlock_<version>_all.deb with plain dpkg-deb (no debhelper needed).
set -eu
cd "$(dirname "$0")"
VERSION=$(python3 -c 'import sys; sys.path.insert(0, "src"); import quickunlock; print(quickunlock.__version__)')
ROOT=build/pkg
rm -rf build && mkdir -p "$ROOT/DEBIAN"

install -d "$ROOT/usr/lib/quickunlock"
cp -r src/quickunlock "$ROOT/usr/lib/quickunlock/"
find "$ROOT" -name __pycache__ -prune -exec rm -rf {} +
install -D -m 0755 libexec/quickunlock-pam     "$ROOT/usr/libexec/quickunlock/quickunlock-pam"
install -D -m 0755 libexec/quickunlock-helper  "$ROOT/usr/libexec/quickunlock/quickunlock-helper"
install -D -m 0755 libexec/quickunlock-helper-restrict "$ROOT/usr/libexec/quickunlock/quickunlock-helper-restrict"
install -D -m 0755 bin/quickunlock-settings    "$ROOT/usr/bin/quickunlock-settings"
install -D -m 0755 bin/quickunlock-pam-setup   "$ROOT/usr/bin/quickunlock-pam-setup"
for m in data/models/*; do install -D -m 0644 "$m" "$ROOT/usr/share/quickunlock/models/$(basename "$m")"; done
install -D -m 0644 data/io.github.quickunlock.QuickUnlock.desktop "$ROOT/usr/share/applications/io.github.quickunlock.QuickUnlock.desktop"
install -D -m 0644 data/io.github.quickunlock.QuickUnlock.metainfo.xml "$ROOT/usr/share/metainfo/io.github.quickunlock.QuickUnlock.metainfo.xml"
install -D -m 0644 data/io.github.quickunlock.QuickUnlock.svg "$ROOT/usr/share/icons/hicolor/scalable/apps/io.github.quickunlock.QuickUnlock.svg"
install -D -m 0644 data/io.github.quickunlock.policy "$ROOT/usr/share/polkit-1/actions/io.github.quickunlock.policy"
install -D -m 0644 LICENSE "$ROOT/usr/share/doc/quickunlock/copyright"
install -D -m 0644 MODELS.md "$ROOT/usr/share/doc/quickunlock/MODELS.md"
install -D -m 0644 README.md "$ROOT/usr/share/doc/quickunlock/README.md"

sed "s/@VERSION@/$VERSION/" debian/control > "$ROOT/DEBIAN/control"
echo "Installed-Size: $(du -sk "$ROOT/usr" | cut -f1)" >> "$ROOT/DEBIAN/control"
for s in postinst prerm postrm; do install -m 0755 "debian/$s" "$ROOT/DEBIAN/$s"; done
install -m 0644 debian/triggers "$ROOT/DEBIAN/triggers"
find "$ROOT" -type d -exec chmod 0755 {} +
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "build/quickunlock_${VERSION}_all.deb"
echo "Built build/quickunlock_${VERSION}_all.deb"

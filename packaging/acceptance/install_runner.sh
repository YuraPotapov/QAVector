#!/bin/bash
# Runs exclusively in a disposable, network-disabled package fixture.
set -euo pipefail
test "${QAVECTOR_OWNED_ACCEPTANCE:-}" = 1
test -r /qa/input/candidate.deb
export DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true
export SUDO_USER=qavector-fixture
gui_pid=
as_user() { setpriv --reuid=1000 --regid=1000 --clear-groups -- "$@"; }
cleanup_gui() {
    if [ -n "$gui_pid" ]; then
        as_user /bin/sh -c 'kill -TERM "$1"' sh "$gui_pid" 2>/dev/null || true
        wait "$gui_pid" 2>/dev/null || true
    fi
}
trap cleanup_gui EXIT

event() { printf '%s\t%s\n' "$1" "$2" >> /qa/out/events.tsv; }
audit() {
    dpkg --audit > "/qa/out/dpkg-audit-$1.txt"
    test ! -s "/qa/out/dpkg-audit-$1.txt"
}
installed() {
    test "$(dpkg-query -W -f='${Status} ${Version}' qavector)" = "install ok installed $1"
}
snapshot() {
    local file
    for file in users.json cycles/sentinel.yaml services.json fixture-secret; do
        sha256sum "$project/$file"
        stat -c '%a %u %g %n' "$project/$file"
    done
}
preserved() {
    snapshot > "/qa/out/data-$1.txt"
    cmp /qa/out/data-before.txt "/qa/out/data-$1.txt"
    event "data-$1" preserved
}
smoke() {
    local stage=$1
    as_user /usr/bin/qavector --version > "/qa/out/core-version-$stage.txt"
    as_user /usr/bin/qavector --runtime-bundle-info > "/qa/out/runtime-$stage.json"
    local component document
    for component in core gui; do
        for document in NOTICE.md AUTHORS COPYING; do
            as_user test -r "/opt/qavector/$component/_internal/licenses/bubblewrap/$document"
        done
    done
    for document in /opt/qavector/core/_internal/runtime/sources/bubblewrap-*.tar.xz; do
        as_user test -r "$document"
    done
    event "offline-licenses-$stage" readable-by-user
    # These modules are used by the GUI and HTML artifact preview. An offscreen
    # startup alone would miss dependencies of the desktop platform plugin.
    local library
    for library in \
        /opt/qavector/gui/_internal/PySide6/Qt/plugins/platforms/libqxcb.so \
        /opt/qavector/gui/_internal/PySide6/Qt/lib/libQt6WebEngineCore.so.6; do
        # Match PyInstaller's library search path: some native libraries are
        # deliberately shipped in _internal rather than required from the OS.
        LD_LIBRARY_PATH=/opt/qavector/gui/_internal:/opt/qavector/gui/_internal/PySide6/Qt/lib \
            ldd "$library" >> "/qa/out/ldd-$stage.txt"
    done
    if grep -q 'not found' "/qa/out/ldd-$stage.txt"; then
        event "libraries-$stage" missing
        return 1
    fi
    event "libraries-$stage" resolved
    as_user mkdir -p "$XDG_CONFIG_HOME/qavector"
    printf '[window]\npage=execution_environments\n' > "$XDG_CONFIG_HOME/qavector/gui.conf"
    chown 1000:1000 "$XDG_CONFIG_HOME/qavector/gui.conf"
    setpriv --reuid=1000 --regid=1000 --clear-groups -- env \
        QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software QTWEBENGINE_DISABLE_SANDBOX=1 \
        timeout --signal=TERM --kill-after=3s 8s /usr/bin/qavector-gui > "/qa/out/gui-$stage.log" 2>&1 &
    gui_pid=$!
    sleep 6
    # kill is a shell builtin on both targets. Debian slim intentionally has no
    # external /bin/kill; do not depend on Ubuntu's installed utility set.
    as_user /bin/sh -c 'kill -0 "$1"' sh "$gui_pid"
    if grep -Eq 'Traceback|ImportError|error while loading shared libraries|Failed to load.*plugin' "/qa/out/gui-$stage.log"; then
        return 1
    fi
    # timeout bounds both startup and shutdown, including the EXIT trap path.
    local gui_status=0
    wait "$gui_pid" || gui_status=$?
    test "$gui_status" = 124
    gui_pid=
    event "gui-$stage" startup-passed
}

dpkg -i /qa/input/dependencies/*.deb
audit dependencies
event dependencies configured

useradd --uid 1000 --create-home qavector-fixture
project=/home/qavector-fixture/QAVector
mkdir -p "$project/cycles"
printf '{"fixture":true}\n' > "$project/users.json"
printf 'id: offline-synthetic-sentinel\n' > "$project/cycles/sentinel.yaml"
printf '{"services":[],"fixture":true}\n' > "$project/services.json"
printf 'SYNTHETIC-NOT-A-CREDENTIAL\n' > "$project/fixture-secret"
chmod 600 "$project/users.json" "$project/fixture-secret"
chown -R 1000:1000 "$project"
snapshot > /qa/out/data-before.txt

# Application startup uses a separate temporary profile. Preservation checks
# cover inert fixture data; no Cycle or Service is executed or migrated.
export HOME=/home/qavector-fixture CMS_HOME=/tmp/qavector-app-fixture
export XDG_CONFIG_HOME=/tmp/qavector-config XDG_DATA_HOME=/tmp/qavector-data
export XDG_RUNTIME_DIR=/tmp/qavector-runtime
export LANG=C.UTF-8 LC_ALL=C.UTF-8
install -d -o 1000 -g 1000 -m 700 "$CMS_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_RUNTIME_DIR"
test "$(as_user id -u)" = 1000
event application-uid 1000

dpkg -i /qa/input/previous.deb
installed "$QAVECTOR_PREVIOUS_VERSION"
audit previous
preserved previous
event previous installed

dpkg -i /qa/input/candidate.deb
installed "$QAVECTOR_CANDIDATE_VERSION"
audit upgrade
preserved upgrade
event upgrade configured
smoke upgrade

dpkg --remove qavector
test ! -e /usr/bin/qavector
test ! -e /usr/bin/qavector-gui
test ! -e /opt/qavector
preserved remove
event remove passed
dpkg --purge qavector
preserved purge
event purge passed

# Fresh candidate installation after a genuine purge exercises that path too.
dpkg -i /qa/input/candidate.deb
installed "$QAVECTOR_CANDIDATE_VERSION"
audit fresh
preserved fresh
smoke fresh
event fresh configured
dpkg --purge qavector
test ! -e /opt/qavector
test ! -e /usr/bin/qavector
preserved final-purge
audit final
event final-purge passed
event acceptance passed

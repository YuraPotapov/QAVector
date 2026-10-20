#!/bin/bash
# Public dependency acquisition only. No application or maintainer script runs.
set -euo pipefail
test "${QAVECTOR_OWNED_ACCEPTANCE:-}" = 1
test -r /qa/input/candidate.deb
test -r /qa/input/previous.deb
export DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8
. /etc/os-release
case "$ID:$VERSION_ID" in ubuntu:22.04|debian:12) ;; *) exit 2 ;; esac
test "$(dpkg --print-architecture)" = amd64
cp /etc/os-release /qa/out/os-release
dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Status}\t${Depends}\t${Pre-Depends}\t${Provides}\n' > /qa/out/base-packages.tsv
sha256sum /var/lib/dpkg/status > /qa/out/dpkg-status-before.txt
mkdir -p /qa/out/apt-sources
if test -f /etc/apt/sources.list; then cp /etc/apt/sources.list /qa/out/apt-sources/; fi
cp -a /etc/apt/sources.list.d /qa/out/apt-sources/
apt-get --error-on=any -o Acquire::Retries=2 -o Acquire::http::Timeout=30 update
# Resolve both genuine archives independently against the unchanged base. No
# forced dependencies, recommends, unauthenticated repositories or host packages.
for archive in /qa/input/previous.deb /qa/input/candidate.deb; do
    apt-get --yes --download-only --no-install-recommends install "$archive"
done
sha256sum /var/lib/dpkg/status > /qa/out/dpkg-status-after.txt
cmp /qa/out/dpkg-status-before.txt /qa/out/dpkg-status-after.txt
cp /var/cache/apt/archives/*.deb /qa/out/
mkdir /qa/out/repository-metadata
for metadata in /var/lib/apt/lists/*InRelease; do
    cp "$metadata" /qa/out/repository-metadata/
done
apt-cache policy > /qa/out/repository-policy.txt
printf 'passed\n' > /qa/out/download-status.txt

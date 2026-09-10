#!/bin/sh
# /usr/bin/qavector -> the bundled launcher.
#
# A wrapper rather than a symlink so the two bundles stay addressable by name
# from anywhere, and so there is somewhere to put environment setup if it is ever
# needed. exec, so signals (the CTRL+C that closes every window) reach the real
# process rather than a shell in between.
exec /opt/qavector/core/qavector-core "$@"

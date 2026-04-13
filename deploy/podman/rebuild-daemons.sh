#!/usr/bin/env bash
# Typo alias: people often type "daemons"; the real script is rebuild-daemon.sh
exec "$(dirname "$0")/rebuild-daemon.sh" "$@"

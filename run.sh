#!/usr/bin/env bash
# Run BlamixFiles from source (macOS, Linux, Git Bash on Windows). Same as: ./dev.sh run
exec "$(dirname "$0")/dev.sh" run "$@"

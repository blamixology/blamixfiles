#!/usr/bin/env bash
# Portable build (Linux, macOS, Git Bash on Windows). Same as: ./dev.sh build
exec "$(dirname "$0")/dev.sh" build "$@"

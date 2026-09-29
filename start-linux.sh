#!/usr/bin/env bash
# QuickTranscriber for Linux - run ./start-linux.sh (first start sets everything up in this folder).
cd "$(dirname "$0")" && exec bash scripts/unix/setup.sh "$@"

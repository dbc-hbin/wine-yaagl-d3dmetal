#!/bin/sh
# Builds the entire current worktree.
# GPTK/D3DMetal and launcher assets remain separate packaging inputs.
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "${WINE_BUILD_PROFILE:-tuned}" in
    tuned) default_root=$repo_dir/build/wine-tuned-current ;;
    safe-msync)
        printf '%s\n' 'build-wine-tuned: WINE_BUILD_PROFILE=safe-msync is not supported; build-wine-p3.sh compiles WINE_SOURCE_DIR (default: this worktree)' >&2
        exit 2 ;;
    *) printf '%s\n' 'build-wine-tuned: unknown WINE_BUILD_PROFILE' >&2; exit 2 ;;
esac
export WINE_P3_ROOT=${WINE_TUNED_ROOT:-${WINE_P3_ROOT:-$default_root}}
export WINE_NATIVE_SERVER=${WINE_NATIVE_SERVER:-yes}
exec "$repo_dir/scripts/build-wine-p3.sh" "$@"

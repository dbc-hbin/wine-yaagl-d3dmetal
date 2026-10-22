#!/bin/sh
# Complete local beta3 operator package. No download, publication or live installation.
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ "$#" -lt 4 ]; then
    cat >&2 <<'EOF'
Usage: scripts/package-wine-p3-runtime.sh HOST GRAPHICS SIDECAR OUTPUT_DIR \
  --dependency-lib X86_64_DEPS/lib --gstreamer-framework SDK/GStreamer.framework \
  --native-fg GAME/amd_fidelityfx_framegeneration_dx12.dll --accept-apple-license

HOST must be build-wine-p3.sh's untouched full-current-source host/ install.
GRAPHICS must be a pristine beta3 donor runtime, including share/yaagl/steam.
SIDECAR must contain the current build-manifest.json and libD3DMetalSidecar.dylib.
Writes OUTPUT_DIR/<build-name>/wine and OUTPUT_DIR/<build-name>.tar.xz + .sha256.
This includes Apple binaries for local use, not authorization to redistribute them.
EOF
    exit 64
fi
host=$1
graphics=$2
sidecar=$3
output=$4
shift 4
name=$(python3 - "$repo_dir" "$host" <<'PY'
import importlib.util, json, pathlib, sys
root, host = map(pathlib.Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('stage_runtime', root / 'scripts/stage-runtime.py')
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
provenance = json.loads((host.resolve().parent / 'provenance.json').read_text())
stage.validate_source_build(provenance)
print(provenance['name'])
PY
)
archive="$output/$name.tar.xz"
staging="$output/$name"
if [ -e "$staging" ] || [ -L "$staging" ] || [ -e "$archive" ] || [ -L "$archive" ] || [ -e "$archive.sha256" ] || [ -L "$archive.sha256" ]; then
    echo "package-wine-p3: refusing existing package output: $staging or $archive" >&2
    exit 1
fi
python3 "$repo_dir/scripts/stage-full-wine-runtime.py" "$host" "$graphics" "$sidecar" "$staging/wine" "$@"
# The full stage is immutable from here; serialize it without macOS resource-fork entries.
partial="$archive.partial.$$"
trap 'rm -f "$partial"' EXIT HUP INT TERM
COPYFILE_DISABLE=1 XZ_OPT='-T0 -3' tar -C "$staging" -cJf "$partial" wine
python3 - "$partial" "$staging/wine/yaagl-wine-runtime-files.json" <<'PY'
import hashlib, json, pathlib, sys, tarfile
manifest_path = pathlib.Path(sys.argv[2])
manifest = json.loads(manifest_path.read_text())
expected = {entry['path']: entry for entry in manifest['entries']}
expected[manifest_path.name] = {'type': 'file', 'sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                                'size': manifest_path.stat().st_size}
seen = set()
with tarfile.open(sys.argv[1], 'r:xz') as archive:
    for member in archive:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'wine':
            raise SystemExit(f'unsafe archive member: {member.name}')
        if member.isdir():
            continue
        relative = '/'.join(path.parts[1:])
        if relative not in expected or relative in seen:
            raise SystemExit(f'unexpected or duplicate archive member: {member.name}')
        seen.add(relative)
        entry = expected[relative]
        if entry['type'] == 'symlink':
            if not member.issym() or member.linkname != entry['target']:
                raise SystemExit(f'archive symlink mismatch: {relative}')
        elif member.isfile():
            stream = archive.extractfile(member)
            value = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                value.update(chunk)
            if member.size != entry['size'] or value.hexdigest() != entry['sha256']:
                raise SystemExit(f'archive file mismatch: {relative}')
        else:
            raise SystemExit(f'unsupported archive member: {relative}')
if seen != expected.keys():
    raise SystemExit('archive is missing runtime manifest entries')
PY
mv "$partial" "$archive"
trap - EXIT HUP INT TERM
shasum -a 256 "$archive" > "$archive.sha256"
printf 'Staged: %s\nArchive: %s\nChecksum: %s\n' "$staging/wine" "$archive" "$archive.sha256"

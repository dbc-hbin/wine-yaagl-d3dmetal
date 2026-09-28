#!/usr/bin/env python3
"""Validated, versioned Wine artifact inventory and profile projections."""

import json
from pathlib import Path
import sys

CATALOG = Path(__file__).with_name('wine-artifacts.json')
PROFILES = {'tuned', 'safe-msync', 'yaagl-overlay'}


def _relative(value):
    if (not isinstance(value, str) or not value or '|' in value or '\n' in value or
            '\r' in value or any(part in ('', '.', '..') for part in value.split('/')) or
            value.startswith('/')):
        raise ValueError(f'invalid catalog path: {value!r}')
    return value


def _paths(values):
    if not isinstance(values, list) or not values:
        raise ValueError('catalog paths must be a nonempty list')
    paths = [_relative(value) for value in values]
    if len(paths) != len(set(paths)):
        raise ValueError('duplicate catalog path')
    return paths


def load_catalog(path=CATALOG):
    data = json.loads(Path(path).read_text())
    if set(data) != {'schemaVersion', 'artifacts', 'profiles'} or data['schemaVersion'] != 1:
        raise ValueError('unsupported artifact catalog schema')
    artifacts = data['artifacts']
    profiles = data['profiles']
    if not isinstance(artifacts, list) or not artifacts or not isinstance(profiles, dict) or set(profiles) != PROFILES:
        raise ValueError('invalid artifact catalog profiles')
    keys = set()
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {'key', 'buildTree', 'makeTarget', 'installedPath', 'architecture', 'format', 'sources'}:
            raise ValueError('invalid artifact record')
        key = item['key']
        if not isinstance(key, str) or not key or key in keys or '|' in key or '\n' in key:
            raise ValueError(f'duplicate or invalid artifact key: {key!r}')
        keys.add(key)
        tree, arch, fmt = item['buildTree'], item['architecture'], item['format']
        target = _relative(item['makeTarget'])
        installed = _relative(item['installedPath'])
        _paths(item['sources'])
        if target == 'server/wineserver':
            expected_installed = 'bin/wineserver'
        else:
            kind = 'windows' if fmt == 'pe' else 'unix'
            expected_installed = f"lib/wine/{arch}-{kind}/{target.rsplit('/', 1)[-1]}"
        if ((tree, arch, fmt) not in {('x86_64', 'x86_64', 'macho'), ('x86_64', 'x86_64', 'pe'),
                                       ('x86_64', 'i386', 'pe'), ('arm64', 'arm64', 'macho')} or
                installed != expected_installed or
                ((tree == 'arm64') != (target == 'server/wineserver')) or
                (fmt == 'pe' and f'/{arch}-windows/' not in '/' + target) or
                (fmt == 'macho' and not (target.endswith('.so') or target == 'server/wineserver'))):
            raise ValueError(f'invalid artifact identity: {key}')
    by_key = {item['key']: item for item in artifacts}
    for name, profile in profiles.items():
        if not isinstance(profile, dict) or set(profile) - {'artifacts', 'buildDirs', 'sourceOverrides', 'sourceInputs'}:
            raise ValueError(f'invalid profile: {name}')
        selected = profile.get('artifacts')
        if (not isinstance(selected, list) or not selected or
                any(not isinstance(key, str) or key not in by_key for key in selected) or
                len(set(selected)) != len(selected)):
            raise ValueError(f'invalid artifact projection: {name}')
        dirs = profile.get('buildDirs')
        if not isinstance(dirs, dict) or set(dirs) != {'x86_64', 'arm64'}:
            raise ValueError(f'invalid build directories: {name}')
        for directory in dirs.values():
            _relative(directory)
        installed = [by_key[key]['installedPath'] for key in selected]
        targets = [(by_key[key]['buildTree'], by_key[key]['makeTarget']) for key in selected]
        if len(set(installed)) != len(installed) or len(set(targets)) != len(targets):
            raise ValueError(f'duplicate installed path or build target: {name}')
        overrides = profile.get('sourceOverrides', {})
        if not isinstance(overrides, dict) or not set(overrides) <= set(selected):
            raise ValueError(f'invalid source overrides: {name}')
        for sources in overrides.values():
            _paths(sources)
        if 'sourceInputs' in profile:
            _paths(profile['sourceInputs'])
    return data


def profile_artifacts(name, catalog=None):
    data = load_catalog() if catalog is None else catalog
    if name not in data['profiles']:
        raise ValueError(f'unknown artifact profile: {name}')
    profile = data['profiles'][name]
    by_key = {item['key']: item for item in data['artifacts']}
    return [{**by_key[key], 'sources': profile.get('sourceOverrides', {}).get(key, by_key[key]['sources'])}
            for key in profile['artifacts']]


def profile_sources(name, catalog=None):
    data = load_catalog() if catalog is None else catalog
    if name not in data['profiles']:
        raise ValueError(f'unknown artifact profile: {name}')
    profile = data['profiles'][name]
    return profile.get('sourceInputs', sorted({path for item in profile_artifacts(name, data)
                                               for path in item['sources']}))


def main():
    if len(sys.argv) not in (3, 4):
        raise SystemExit('usage: wine_artifacts.py rows|sources|targets|build-dir PROFILE [BUILD_TREE]')
    operation, name = sys.argv[1:3]
    data = load_catalog()
    artifacts = profile_artifacts(name, data)
    if operation == 'rows' and len(sys.argv) == 3:
        for item in artifacts:
            print('|'.join((item['key'], item['buildTree'], item['makeTarget'],
                            item['installedPath'], item['architecture'], item['format'],
                            ','.join(item['sources']))))
    elif operation == 'sources' and len(sys.argv) == 3:
        print('\n'.join(profile_sources(name, data)))
    elif operation == 'targets' and len(sys.argv) == 4 and sys.argv[3] in ('x86_64', 'arm64'):
        print(' '.join(item['makeTarget'] for item in artifacts if item['buildTree'] == sys.argv[3]))
    elif operation == 'build-dir' and len(sys.argv) == 4 and sys.argv[3] in ('x86_64', 'arm64'):
        print(data['profiles'][name]['buildDirs'][sys.argv[3]])
    else:
        raise SystemExit('invalid catalog operation')


if __name__ == '__main__':
    main()

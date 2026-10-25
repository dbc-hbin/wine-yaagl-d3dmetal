#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Exercise the session supervisor against a local machine-state fake, never Apple policy.

Every lifetime transition is driven by a pipe or process exit. No games, global
policy commands, polling or timing sleeps are used.
"""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def receive(stream):
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        assert selector.select(10), 'supervisor readiness timed out'
        return json.loads(stream.readline())


def build_test_supervisor(root, binary, fake_signature=True):
    state = root / 'machine.json'
    fake = binary.parent / 'gamepolicyctl'
    fake.write_text(f'''#!{sys.executable}
import json
from pathlib import Path
import sys
state = Path({str(state)!r})
data = json.loads(state.read_text())
args = sys.argv[1:]
assert args[:1] == ['game-mode'], args
if args[1] == 'set':
    assert len(args) == 3, args
    policy = args[2]
    data['writes'].append(policy)
    if data.get('fail_restore') and policy != 'on':
        state.write_text(json.dumps(data))
        sys.exit(1)
    data['policy'] = policy
    state.write_text(json.dumps(data))
    if data.get('fail_set') and policy == 'on':
        sys.exit(1)
else:
    assert args[1:] == ['status'], args
    enabled = data['policy'] == 'on' and not data.get('inactive')
    # Apple's CLI retains these SGR sequences even with redirected stdout.
    color = '\\033[0;32m' if enabled else '\\033[0;31m'
    print('Game mode is ' + color + ('on' if enabled else 'off') + '\\033[0;0m.\\033[0;33m\\033[0;0m')
    names = {{'auto': 'automatic',
             'on': 'disabled. Game mode is forced always on',
             'off': 'disabled. Game mode is forced always off'}}
    print('Game mode enablement policy is currently ' + names[data['policy']] + '.')
''')
    fake.chmod(0o700)
    state.write_text(json.dumps(dict(policy='auto', writes=[])))
    interleave = root / 'interleave.h'
    interleave.write_text(r'''
#include <sys/file.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static int observed_flock(int fd, int operation)
{
    int result = flock(fd, operation);
    const char *ready = getenv("POLICY_TEST_RELEASE_READY");
    struct stat st;
    char path[1024], byte;
    if (!result && operation == LOCK_UN && ready &&
        !fcntl(fd, F_GETPATH, path) && strstr(path, "/policy") &&
        !fstat(fd, &st) && !st.st_size)
    {
        write(atoi(ready), "1", 1);
        read(atoi(getenv("POLICY_TEST_RELEASE_CONTINUE")), &byte, 1);
    }
    return result;
}
#define flock observed_flock
''')
    if fake_signature:
        # Substitute only the trust boundary for the script fixture. Executable
        # discovery, regular-file checks, argv and spawning remain production code.
        with interleave.open('a') as stream:
            stream.write(r'''
#include <Security/Security.h>
static OSStatus fixture_code(CFURLRef url, SecCSFlags flags, SecStaticCodeRef *code)
{
    (void)flags;
    *code = (SecStaticCodeRef)CFRetain(url);
    return errSecSuccess;
}
static OSStatus fixture_validity(SecStaticCodeRef code, SecCSFlags flags, SecRequirementRef requirement)
{
    (void)code;
    (void)flags;
    (void)requirement;
    return errSecSuccess;
}
static OSStatus fixture_information(SecStaticCodeRef code, SecCSFlags flags, CFDictionaryRef *information)
{
    (void)code;
    (void)flags;
    const void *keys[] = {CFSTR("com.apple.gamepolicyd.tool.privileged.xpc")};
    const void *values[] = {kCFBooleanTrue};
    CFDictionaryRef entitlements = CFDictionaryCreate(NULL, keys, values, 1,
                                                      &kCFTypeDictionaryKeyCallBacks,
                                                      &kCFTypeDictionaryValueCallBacks);
    const void *info_keys[] = {kSecCodeInfoEntitlementsDict};
    const void *info_values[] = {entitlements};
    *information = CFDictionaryCreate(NULL, info_keys, info_values, 1,
                                      &kCFTypeDictionaryKeyCallBacks,
                                      &kCFTypeDictionaryValueCallBacks);
    CFRelease(entitlements);
    return errSecSuccess;
}
#define SecStaticCodeCreateWithPath fixture_code
#define SecStaticCodeCheckValidity fixture_validity
#define SecCodeCopySigningInformation fixture_information
''')
    subprocess.run(['/usr/bin/xcrun', 'clang', '-O0', '-g', '-Wall', '-Wextra', '-Werror',
                    '-include', str(interleave),
                    '-DGAME_POLICY_TEST_DIRECTORY=' + json.dumps(str(root / 'leases')),
                    '-framework', 'Security', '-framework', 'CoreFoundation',
                    str(ROOT / 'scripts/game-policy-supervisor.c'), '-o', str(binary)], check=True)
    return state


def main():
    with tempfile.TemporaryDirectory(prefix='yaagl-policy-test-') as temporary:
        root = Path(temporary)
        original = root / 'original helpers'
        original.mkdir()
        binary = original / 'supervisor'
        state = build_test_supervisor(root, binary)
        relocated = root / 'relocated runtime with spaces'
        original.rename(relocated)
        binary = relocated / binary.name
        fake = relocated / 'gamepolicyctl'
        supervisors = []
        wine, server = root / 'wine with spaces', root / 'wineserver'
        for path, phase, arguments in ((wine, 'wine', ['game.exe', 'argument with spaces']),
                                       (server, 'server', ['-w'])):
            path.write_text(f'''#!{sys.executable}
import json
import os
import sys
assert sys.argv[1:] == {arguments!r}
assert os.environ['SESSION_TEST_ENV'] == 'inherited game environment'
os.write(1, (json.dumps(dict(phase={phase!r}, pid=os.getpid())) + '\\n').encode())
os.read(0, 1)
sys.exit(int(os.environ.get('SESSION_TEST_{phase.upper()}_EXIT', '0')))
''')
            path.chmod(0o700)

        def machine(policy='auto', **faults):
            state.write_text(json.dumps(dict(policy=policy, writes=[], **faults)))

        def snapshot():
            return json.loads(state.read_text())

        def acquire(success=True, executable=None, argv0=None, env=None, pass_fds=()):
            executable = executable or binary
            helper = subprocess.Popen(
                [argv0 or str(executable), str(server), str(wine), 'game.exe', 'argument with spaces'],
                executable=str(executable), cwd='/',
                env={**os.environ, 'SESSION_TEST_ENV': 'inherited game environment', **(env or {})},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, pass_fds=pass_fds)
            supervisors.append(helper)
            event = receive(helper.stdout)
            assert event['phase'] == 'wine'
            if success:
                assert snapshot()['policy'] == 'on'
            return helper, event['pid']

        def finish(helper, wine_pid, sig=None, result=0, wait=True):
            before = snapshot()
            if sig:
                os.kill(wine_pid, sig)
            else:
                helper.stdin.write(b'w')
            event = receive(helper.stdout)
            assert event['phase'] == 'server'
            assert snapshot() == before, 'policy restored before Wine session drained'
            helper.stdin.close()
            if wait:
                assert helper.wait(timeout=10) == result, helper.stderr.read().decode()

        try:
            # A caller-provided argv[0], working directory and developer search
            # environment must not affect executable-relative discovery.
            machine()
            helper, wine_pid = acquire(argv0='not-the-supervisor',
                env={'PATH': '/nonexistent', 'DEVELOPER_DIR': '/nonexistent'})
            finish(helper, wine_pid)
            assert snapshot() == dict(policy='auto', writes=['on', 'auto'])
            print('PASS relocated executable discovery and native argv')

            for invalid in ('missing', 'symlink'):
                machine()
                saved = fake.with_name('saved-gamepolicyctl')
                fake.rename(saved)
                if invalid == 'symlink':
                    fake.symlink_to(saved.name)
                try:
                    helper, wine_pid = acquire(success=False)
                    finish(helper, wine_pid)
                    assert snapshot() == dict(policy='auto', writes=[])
                finally:
                    if fake.is_symlink():
                        fake.unlink()
                    saved.rename(fake)
                print('PASS rejected sibling:', invalid)

            untrusted = root / 'untrusted runtime'
            untrusted.mkdir()
            production = untrusted / 'supervisor'
            build_test_supervisor(root, production, fake_signature=False)
            helper, wine_pid = acquire(success=False, executable=production)
            finish(helper, wine_pid)
            assert snapshot() == dict(policy='auto', writes=[])
            print('PASS production trust rejects unsigned sibling')

            for prior in ('auto', 'on', 'off'):
                machine(prior)
                helper, wine_pid = acquire()
                finish(helper, wine_pid)
                data = snapshot()
                assert data['policy'] == prior
                assert data['writes'] == ([] if prior == 'on' else ['on', prior])
                print('PASS prior policy:', prior)

            for sig in (signal.SIGTERM, signal.SIGKILL):
                machine()
                helper, wine_pid = acquire()
                finish(helper, wine_pid, sig, result=128 + sig)
                assert snapshot()['policy'] == 'auto'
                print('PASS Wine command termination:', sig)

            for phase in ('WINE', 'SERVER'):
                machine()
                helper, wine_pid = acquire(env={f'SESSION_TEST_{phase}_EXIT': '7'})
                finish(helper, wine_pid, result=7)
                assert snapshot() == dict(policy='auto', writes=['on', 'auto'])
                print('PASS command failure and restoration:', phase)

            machine('off')
            first_helper, first = acquire()
            second_helper, second = acquire()
            finish(first_helper, first)
            assert snapshot() == dict(policy='on', writes=['on'])
            finish(second_helper, second)
            assert snapshot() == dict(policy='off', writes=['on', 'off'])
            print('PASS overlapping owners')

            machine()
            ready_read, ready_write = os.pipe()
            continue_read, continue_write = os.pipe()
            try:
                helper, wine_pid = acquire(
                    env={'POLICY_TEST_RELEASE_READY': str(ready_write),
                         'POLICY_TEST_RELEASE_CONTINUE': str(continue_read)},
                    pass_fds=(ready_write, continue_read))
                finish(helper, wine_pid, wait=False)
                with selectors.DefaultSelector() as selector:
                    selector.register(ready_read, selectors.EVENT_READ)
                    assert selector.select(10)
                    assert os.read(ready_read, 1) == b'1'
                # The old helper is paused immediately after releasing the
                # transaction gate, before descriptor cleanup can hide a race.
                second_helper, second = acquire()
                assert snapshot()['policy'] == 'on'
                os.write(continue_write, b'1')
                assert helper.wait(timeout=10) == 0
                finish(second_helper, second)
                assert snapshot() == dict(policy='auto', writes=['on', 'auto', 'on', 'auto'])
                print('PASS terminal owner and new acquisition interleave')
            finally:
                os.close(continue_write)
                os.close(continue_read)
                os.close(ready_write)
                os.close(ready_read)

            for fault in ('inactive', 'fail_set'):
                machine('auto', **{fault: True})
                helper, wine_pid = acquire(success=False)
                finish(helper, wine_pid)
                assert snapshot()['policy'] == 'auto'
                assert snapshot()['writes'] == ['on', 'auto']
                print('PASS acquisition rollback:', fault)

            machine('auto')
            helper, wine_pid = acquire()
            data = snapshot()
            data['policy'] = 'off'
            state.write_text(json.dumps(data))
            finish(helper, wine_pid)
            assert snapshot()['policy'] == 'off'
            assert snapshot()['writes'] == ['on']
            print('PASS external policy preserved')

            machine('auto')
            helper, wine_pid = acquire()
            data = snapshot()
            data['policy'] = 'off'
            state.write_text(json.dumps(data))
            other_helper, other = acquire(success=False)
            finish(other_helper, other)
            data['policy'] = 'on'
            state.write_text(json.dumps(data))
            finish(helper, wine_pid)
            assert snapshot()['policy'] == 'on'
            assert snapshot()['writes'] == ['on']
            print('PASS observed external change relinquishes restoration')

            machine('off', fail_restore=True)
            helper, wine_pid = acquire()
            finish(helper, wine_pid, result=1)
            assert (root / 'leases/policy').read_bytes() == b'f'
            data = snapshot()
            data.pop('fail_restore')
            state.write_text(json.dumps(data))
            helper, wine_pid = acquire()
            finish(helper, wine_pid)
            assert snapshot()['policy'] == 'off'
            assert (root / 'leases/policy').read_bytes() == b''
            print('PASS failed cleanup retains recovery record')

            machine()
            helper = subprocess.Popen(
                [str(binary), str(server), str(root / 'missing-wine')],
                env={**os.environ, 'SESSION_TEST_ENV': 'inherited game environment'},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            supervisors.append(helper)
            assert receive(helper.stdout)['phase'] == 'server'
            assert snapshot()['policy'] == 'on'
            helper.stdin.close()
            assert helper.wait(timeout=10) == 127
            assert snapshot() == dict(policy='auto', writes=['on', 'auto'])
            print('PASS failed Wine spawn still drains session and restores policy')
        finally:
            for helper in supervisors:
                if not helper.stdin.closed:
                    helper.stdin.close()
                try:
                    helper.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    helper.kill()
                    helper.wait(timeout=10)
                helper.stdout.close()
                helper.stderr.close()


if __name__ == '__main__':
    main()

/* SPDX-License-Identifier: LGPL-2.1-or-later
 * A Wine-session lease on Apple's global Game Mode override.
 * Only the bundled, unmodified Apple command changes policy.
 */
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <Security/Security.h>
#include <signal.h>
#include <spawn.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static char policy_command[PATH_MAX];
extern char **environ;

static int resolve_policy_command(void)
{
    char executable[PATH_MAX], *slash;
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath( executable, &size ) || !realpath( executable, policy_command )) return -1;
    if (!(slash = strrchr( policy_command, '/' ))) return -1;
    if ((size_t)(slash + 1 - policy_command) + sizeof("gamepolicyctl") > sizeof(policy_command)) return -1;
    strcpy( slash + 1, "gamepolicyctl" );
    return 0;
}

static int trusted_policy_command(void)
{
    struct stat st;
    CFURLRef url;
    CFDictionaryRef information = NULL, entitlements;
    SecStaticCodeRef code = NULL;
    SecRequirementRef requirement = NULL;
    OSStatus result;
    if (lstat( policy_command, &st ) || !S_ISREG(st.st_mode) || access( policy_command, X_OK )) return -1;
    url = CFURLCreateFromFileSystemRepresentation( NULL, (const UInt8 *)policy_command,
                                                  strlen(policy_command), false );
    if (!url) return -1;
    result = SecStaticCodeCreateWithPath( url, kSecCSDefaultFlags, &code );
    if (!result) result = SecRequirementCreateWithString(
        CFSTR("identifier \"com.apple.gamepolicyctl\" and anchor apple"),
        kSecCSDefaultFlags, &requirement );
    if (!result) result = SecStaticCodeCheckValidity( code, kSecCSStrictValidate, requirement );
    if (!result) result = SecCodeCopySigningInformation( code, kSecCSSigningInformation, &information );
    if (!result)
    {
        entitlements = CFDictionaryGetValue( information, kSecCodeInfoEntitlementsDict );
        if (!entitlements || CFGetTypeID( entitlements ) != CFDictionaryGetTypeID() ||
            CFDictionaryGetValue( entitlements, CFSTR("com.apple.gamepolicyd.tool.privileged.xpc") ) != kCFBooleanTrue)
            result = errSecCSReqFailed;
    }
    if (information) CFRelease( information );
    if (requirement) CFRelease( requirement );
    if (code) CFRelease( code );
    CFRelease( url );
    return result ? -1 : 0;
}

/* Return the command's complete output, never a truncated success. */
static int command( const char *policy, char *output, size_t capacity )
{
    int pipes[2], status, error;
    pid_t child, waited;
    posix_spawnattr_t attributes;
    posix_spawn_file_actions_t actions;
    char *const environment[] = { "PATH=/usr/bin:/bin", "LC_ALL=C", NULL };
    char *const arguments[] = { policy_command, "game-mode",
                               policy ? "set" : "status", (char *)policy, NULL };
    ssize_t count;
    size_t used = 0;
    char discard[256];
    if (trusted_policy_command()) return -1;
    if (pipe( pipes )) return -1;
    if (posix_spawnattr_init( &attributes )) goto failed;
    if (posix_spawn_file_actions_init( &actions )) goto failed_attributes;
    error = posix_spawnattr_setflags( &attributes, POSIX_SPAWN_CLOEXEC_DEFAULT );
    if (!error) error = posix_spawn_file_actions_addopen( &actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0 );
    if (!error) error = posix_spawn_file_actions_adddup2( &actions, pipes[1], STDOUT_FILENO );
    if (!error && pipes[1] != STDOUT_FILENO) error = posix_spawn_file_actions_addclose( &actions, pipes[1] );
    if (!error) error = posix_spawn_file_actions_addinherit_np( &actions, STDERR_FILENO );
    if (!error) error = posix_spawn( &child, policy_command, &actions, &attributes,
                                   arguments, environment );
    posix_spawn_file_actions_destroy( &actions );
    posix_spawnattr_destroy( &attributes );
    if (error) goto failed;
    close( pipes[1] );
    for (;;)
    {
        count = read( pipes[0], used < capacity - 1 ? output + used : discard,
                      used < capacity - 1 ? capacity - 1 - used : sizeof(discard) );
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) break;
        used += count;
    }
    close( pipes[0] );
    do { waited = waitpid( child, &status, 0 ); } while (waited < 0 && errno == EINTR);
    if (count < 0 || used >= capacity || waited < 0 || !WIFEXITED(status) || WEXITSTATUS(status)) return -1;
    output[used] = 0;
    return 0;
failed_attributes:
    posix_spawnattr_destroy( &attributes );
failed:
    close( pipes[0] );
    close( pipes[1] );
    return -1;
}

/* Unknown output is a failure, not an assumption that policy is automatic. */
static int status_policy( char *policy, int *enabled )
{
    char output[4096], *source, *target;
    if (command( NULL, output, sizeof(output) )) return -1;
    /* gamepolicyctl emits SGR colors even when stdout is a pipe. */
    for (source = target = output; *source; source++)
    {
        if (*source == '\033')
        {
            if (source[1] != '[') return -1;
            source += 2 + strspn( source + 2, "0123456789;" );
            if (*source != 'm') return -1;
        }
        else *target++ = *source;
    }
    *target = 0;
    if (strstr( output, "Game mode is on." )) *enabled = 1;
    else if (strstr( output, "Game mode is off." )) *enabled = 0;
    else return -1;
    if (strstr( output, "enablement policy is currently automatic." )) *policy = 'a';
    else if (strstr( output, "enablement policy is currently disabled. Game mode is forced always on." )) *policy = 'n';
    else if (strstr( output, "enablement policy is currently disabled. Game mode is forced always off." )) *policy = 'f';
    else return -1;
    return 0;
}

static int set_policy( char policy )
{
    char output[4096];
    return command( policy == 'a' ? "auto" : policy == 'n' ? "on" : "off", output, sizeof(output) );
}

static int lock_file( int directory, const char *name )
{
    struct stat st;
    int fd = openat( directory, name, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0600 );
    if (fd < 0) return -1;
    if (fstat( fd, &st ) || !S_ISREG(st.st_mode) || st.st_uid != getuid() ||
        (st.st_mode & 077) || st.st_nlink != 1)
    {
        close( fd );
        return -1;
    }
    return fd;
}

/* Caller holds the transaction lock and an exclusive lifetime lock. Keep the
 * recovery record on failure; a later session must recover before acquiring. */
static int restore( int state )
{
    char prior, current;
    int enabled;
    ssize_t size = pread( state, &prior, 1, 0 );
    if (!size) return 0;
    if (size != 1 || (prior != 'a' && prior != 'n' && prior != 'f')) return -1;
    if (status_policy( &current, &enabled )) return -1;
    if (current == 'n' && prior != 'n')
    {
        if (set_policy( prior ) || status_policy( &current, &enabled ) || current != prior) return -1;
    }
    /* A different observable policy belongs to the user, not to this lease. */
    return ftruncate( state, 0 ) || fsync( state ) ? -1 : 0;
}

static int run( char *const argv[] )
{
    const int reset_signals[] = { SIGPIPE, SIGHUP, SIGINT, SIGQUIT, SIGTERM };
    posix_spawnattr_t attributes;
    posix_spawn_file_actions_t actions;
    sigset_t defaults, mask;
    pid_t child, waited;
    int error, status;
    if (posix_spawnattr_init( &attributes )) return 127;
    if (posix_spawn_file_actions_init( &actions ))
    {
        posix_spawnattr_destroy( &attributes );
        return 127;
    }
    sigemptyset( &defaults );
    sigemptyset( &mask );
    for (size_t i = 0; i < sizeof(reset_signals) / sizeof(reset_signals[0]); ++i)
        sigaddset( &defaults, reset_signals[i] );
    error = posix_spawnattr_setsigdefault( &attributes, &defaults );
    if (!error) error = posix_spawnattr_setsigmask( &attributes, &mask );
    if (!error) error = posix_spawnattr_setflags( &attributes,
        POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK );
    for (int fd = STDIN_FILENO; !error && fd <= STDERR_FILENO; ++fd)
        error = posix_spawn_file_actions_addinherit_np( &actions, fd );
    if (!error) error = posix_spawn( &child, argv[0], &actions, &attributes, argv, environ );
    posix_spawn_file_actions_destroy( &actions );
    posix_spawnattr_destroy( &attributes );
    if (error)
    {
        fprintf( stderr, "wine: could not start %s: %s\n", argv[0], strerror(error) );
        return 127;
    }
    do { waited = waitpid( child, &status, 0 ); } while (waited < 0 && errno == EINTR);
    if (waited < 0) return 127;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}

int main( int argc, char **argv )
{
    char directory_name[256], current, prior;
    struct stat st;
    int directory = -1, gate = -1, lifetime = -1, enabled, first, result, server_result;
    bool acquired = false;
    char *server_argv[] = { NULL, "-w", NULL };
    if (argc < 3 || argv[1][0] != '/' || argv[2][0] != '/') return 2;
    server_argv[0] = argv[1];
    /* Keep the supervisor alive to restore policy when a child is terminated.
     * Spawned Wine/server commands receive normal signal dispositions.
     */
    signal( SIGPIPE, SIG_IGN );
    signal( SIGHUP, SIG_IGN );
    signal( SIGINT, SIG_IGN );
    signal( SIGQUIT, SIG_IGN );
    signal( SIGTERM, SIG_IGN );
    if (resolve_policy_command()) goto unavailable;
#ifdef GAME_POLICY_TEST_DIRECTORY
    snprintf( directory_name, sizeof(directory_name), "%s", GAME_POLICY_TEST_DIRECTORY );
#else
    snprintf( directory_name, sizeof(directory_name), "/tmp/yaagl-game-policy-%lu", (unsigned long)getuid() );
#endif
    if (mkdir( directory_name, 0700 ) && errno != EEXIST) goto unavailable;
    directory = open( directory_name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC );
    if (directory < 0 || fstat( directory, &st ) || st.st_uid != getuid() || (st.st_mode & 077)) goto unavailable;
    gate = lock_file( directory, "policy" );
    lifetime = lock_file( directory, "owners" );
    if (gate < 0 || lifetime < 0 || flock( gate, LOCK_EX )) goto unavailable;
    first = !flock( lifetime, LOCK_EX | LOCK_NB );
    if (!first && errno != EWOULDBLOCK) goto unavailable;
    if (first)
    {
        if (restore( gate ) || status_policy( &prior, &enabled )) goto unavailable;
        if (pwrite( gate, &prior, 1, 0 ) != 1 || fsync( gate )) goto unavailable;
        if ((prior != 'n' && set_policy( 'n' )) || status_policy( &current, &enabled ) || current != 'n' || !enabled)
        {
            if (restore( gate )) fprintf( stderr, "wine: Game Mode policy cleanup failed; recovery record retained\n" );
            goto unavailable;
        }
    }
    else
    {
        if (status_policy( &current, &enabled )) goto unavailable;
        if (current != 'n')
        {
            /* Record an observed external change even if the user subsequently
             * chooses on again before the last owner exits. */
            if (ftruncate( gate, 0 ) || fsync( gate ))
                fprintf( stderr, "wine: Game Mode external-policy record update failed\n" );
            goto unavailable;
        }
        if (!enabled) goto unavailable;
    }
    if (flock( lifetime, LOCK_SH ))
    {
        if (first && restore( gate ))
            fprintf( stderr, "wine: Game Mode policy cleanup failed; recovery record retained\n" );
        goto unavailable;
    }
    flock( gate, LOCK_UN );
    acquired = true;
    fprintf( stderr, "wine: Game Mode global policy acquired for Wine session\n" );
    goto session;
unavailable:
    if (lifetime >= 0) { close( lifetime ); lifetime = -1; }
    if (gate >= 0) { close( gate ); gate = -1; }
    if (directory >= 0) { close( directory ); directory = -1; }
    fprintf( stderr, "wine: Game Mode policy unavailable; continuing Wine session\n" );
session:
    result = run( argv + 2 );
    server_result = run( server_argv );
    if (!result) result = server_result;
    if (!acquired) return result;
    if (flock( gate, LOCK_EX )) { if (!result) result = 1; goto done; }
    flock( lifetime, LOCK_UN );
    if (!flock( lifetime, LOCK_EX | LOCK_NB ))
    {
        if (restore( gate ))
        {
            fprintf( stderr, "wine: Game Mode policy cleanup failed; recovery record retained\n" );
            if (!result) result = 1;
        }
    }
    else if (errno != EWOULDBLOCK && !result) result = 1;
    /* A new owner must not see a terminal helper as an active lease after
     * the previous policy has already been restored. */
    flock( lifetime, LOCK_UN );
    flock( gate, LOCK_UN );
done:
    if (lifetime >= 0) close( lifetime );
    if (gate >= 0) close( gate );
    if (directory >= 0) close( directory );
    return result;
}

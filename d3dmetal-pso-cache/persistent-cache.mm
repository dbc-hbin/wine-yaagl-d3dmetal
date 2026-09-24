#include "persistent-cache.hpp"

#include <dirent.h>
#include <fcntl.h>
#include <limits.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <string>

namespace yaagl::pso {
namespace {

constexpr std::uint64_t kTotalAdviceBudget = 8ULL * 1024 * 1024;
constexpr std::uint64_t kCacheAdviceLimit = 1024ULL * 1024;
constexpr std::uint64_t kVersionAdviceLimit = 64ULL * 1024;
constexpr std::size_t kDirectoryScanLimit = 64;
constexpr std::array<const char*, 4> kCacheNames = {
    "bytecode_cache.bin",
    "rootsignature_cache.bin",
    "stage_cache.bin",
    "pipeline_cache.bin",
};

class FileDescriptor final {
public:
    explicit FileDescriptor(int value = -1) noexcept : value_(value) {}
    ~FileDescriptor() { if (value_ >= 0) close(value_); }
    FileDescriptor(const FileDescriptor&) = delete;
    FileDescriptor& operator=(const FileDescriptor&) = delete;
    int get() const noexcept { return value_; }
    int release() noexcept {
        const int value = value_;
        value_ = -1;
        return value;
    }
private:
    int value_;
};

bool safeLeaf(const char* value) noexcept {
    if (value == nullptr || value[0] == '\0' || std::strcmp(value, ".") == 0 ||
        std::strcmp(value, "..") == 0) return false;
    const std::size_t length = std::strlen(value);
    if (length > NAME_MAX) return false;
    for (std::size_t index = 0; index < length; ++index) {
        if (value[index] == '/') return false;
    }
    return true;
}

bool gpuDirectoryName(const char* name) noexcept {
    constexpr char prefix[] = "MTLGPUFamily";
    if (!safeLeaf(name) || std::strncmp(name, prefix, sizeof(prefix) - 1) != 0) return false;
    for (const char* cursor = name + sizeof(prefix) - 1; *cursor != '\0'; ++cursor) {
        const unsigned char value = static_cast<unsigned char>(*cursor);
        if (!((value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z') ||
              (value >= '0' && value <= '9') || value == '_')) return false;
    }
    return name[sizeof(prefix) - 1] != '\0';
}

int openDirectoryAt(int parent, const char* name) noexcept {
    return openat(parent, name, O_RDONLY | O_CLOEXEC | O_DIRECTORY | O_NOFOLLOW);
}

void adviseFile(int directory, const char* name, std::uint64_t perFileLimit,
                std::uint64_t& budget, PersistentCacheWarmupResult& result) noexcept {
    if (budget == 0) return;
    FileDescriptor file(openat(directory, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK));
    if (file.get() < 0) return;
    struct stat status {};
    if (fstat(file.get(), &status) != 0 || !S_ISREG(status.st_mode) || status.st_size <= 0) return;
    const auto fileSize = static_cast<std::uint64_t>(status.st_size);
    const std::uint64_t count = std::min({fileSize, perFileLimit, budget,
        static_cast<std::uint64_t>(INT_MAX)});
    if (count == 0) return;
    radvisory advice {0, static_cast<int>(count)};
    if (fcntl(file.get(), F_RDADVISE, &advice) != 0) return;
    ++result.filesAdvised;
    result.bytesAdvised += count;
    budget -= count;
}

void warmGpuDirectory(int directory, std::uint64_t& budget,
                      PersistentCacheWarmupResult& result) noexcept {
    adviseFile(directory, "version.bin", kVersionAdviceLimit, budget, result);
    for (const char* name : kCacheNames) {
        adviseFile(directory, name, kCacheAdviceLimit, budget, result);
    }
}

const char* executableLeaf(const char* path) noexcept {
    if (path == nullptr) return nullptr;
    const char* slash = std::strrchr(path, '/');
    const char* backslash = std::strrchr(path, '\\');
    const char* separator = slash == nullptr ? backslash :
        (backslash == nullptr || slash > backslash ? slash : backslash);
    return separator == nullptr ? path : separator + 1;
}

const char* currentExecutableLeaf() noexcept {
    const char* processName = executableLeaf(getprogname());
    return safeLeaf(processName) ? processName : nullptr;
}

} // namespace

static PersistentCacheWarmupResult warmPersistentCachesAtImpl(
    const char* cacheRoot, const char* executableName) noexcept {
    PersistentCacheWarmupResult result;
    try {
        if (cacheRoot == nullptr || cacheRoot[0] != '/' || !safeLeaf(executableName)) return result;
        FileDescriptor root(open(cacheRoot, O_RDONLY | O_CLOEXEC | O_DIRECTORY | O_NOFOLLOW));
        if (root.get() < 0) return result;
        FileDescriptor d3dm(openDirectoryAt(root.get(), "d3dm"));
        if (d3dm.get() < 0) return result;
        FileDescriptor executable(openDirectoryAt(d3dm.get(), executableName));
        if (executable.get() < 0) return result;
        FileDescriptor cache(openDirectoryAt(executable.get(), "shaders.cache"));
        if (cache.get() < 0) return result;

        FileDescriptor enumerationFd(dup(cache.get()));
        if (enumerationFd.get() < 0) return result;
        DIR* rawDirectory = fdopendir(enumerationFd.get());
        if (rawDirectory == nullptr) return result;
        static_cast<void>(enumerationFd.release());
        std::uint64_t budget = kTotalAdviceBudget;
        std::size_t scanned = 0;
        while (budget != 0 && scanned < kDirectoryScanLimit) {
            errno = 0;
            dirent* entry = readdir(rawDirectory);
            if (entry == nullptr) break;
            ++scanned;
            if (!gpuDirectoryName(entry->d_name)) continue;
            struct stat status {};
            if (fstatat(cache.get(), entry->d_name, &status, AT_SYMLINK_NOFOLLOW) != 0 ||
                !S_ISDIR(status.st_mode)) continue;
            FileDescriptor gpu(openDirectoryAt(cache.get(), entry->d_name));
            if (gpu.get() < 0) continue;
            ++result.directoriesVisited;
            warmGpuDirectory(gpu.get(), budget, result);
        }
        closedir(rawDirectory);
    } catch (...) {
        // Cache warming is advisory and must never affect process startup.
    }
    return result;
}

PersistentCacheWarmupResult warmPersistentCachesFromEnvironment() noexcept {
    const char* enabled = std::getenv("YAAGL_D3DMETAL_CACHE_WARMUP");
    if (enabled == nullptr || std::strcmp(enabled, "1") != 0) return {};
    try {
        std::string root;
        const std::size_t length = confstr(_CS_DARWIN_USER_CACHE_DIR, nullptr, 0);
        if (length == 0 || length > PATH_MAX) return {};
        root.resize(length);
        if (confstr(_CS_DARWIN_USER_CACHE_DIR, root.data(), root.size()) != length) return {};
        if (!root.empty() && root.back() == '\0') root.pop_back();
        const char* executable = currentExecutableLeaf();
        return warmPersistentCachesAtImpl(root.c_str(), executable);
    } catch (...) {
        return {};
    }
}

} // namespace yaagl::pso

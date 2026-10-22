#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <type_traits>

namespace yaagl::native {

// These are wire layouts, not replicas of the native C++ classes. Their scope
// is the supported GPTK image, checked before any private field is accessed.
struct TextureView {
    std::uint16_t firstMip = 0;
    std::uint16_t mipCount = 1;
    std::uint16_t firstSlice = 0;
    std::uint16_t sliceCount = 1;
    std::uint8_t planes = 1;
    std::uint8_t padding[7]{};
};
static_assert(sizeof(TextureView) == 16);

struct MplListPrefix { void* context; void* allocator; };
struct MplContextPrefix { void* device; void* reserved; void* compiler; };
struct MplAllocatorPrefix { void* context; };

template<class T> T readObject(const void* pointer) noexcept {
    static_assert(std::is_trivially_copyable_v<T>);
    T value{};
    if (pointer) std::memcpy(&value, pointer, sizeof(value));
    return value;
}

struct MplState {
    void* list = nullptr;
    void* context = nullptr;
    void* allocator = nullptr;
    void* device = nullptr;
    void* compiler = nullptr;
};

// The caller supplies a raw list from the verified TemporalScale entry or an
// MDE commandList getter. Identity comes from that provenance and ownership
// edges, never from allocator hash-table load factors or bucket counts.
inline bool describeMpl(void* list, MplState& state, const char** reason = nullptr) noexcept {
    state = {};
    if (reason) *reason = nullptr;
    const auto fail = [reason](const char* message) {
        if (reason) *reason = message;
        return false;
    };
    if (!list) return fail("missing-mpl-list");
    const auto prefix = readObject<MplListPrefix>(list);
    if (!prefix.context) return fail("missing-mpl-context");
    if (!prefix.allocator) return fail("missing-mpl-allocator");
    if (readObject<MplAllocatorPrefix>(prefix.allocator).context != prefix.context)
        return fail("allocator-context-mismatch");
    const auto context = readObject<MplContextPrefix>(prefix.context);
    if (!context.device) return fail("missing-metal-device");
    if (!context.compiler) return fail("missing-metal-compiler");
    state = {list, prefix.context, prefix.allocator, context.device, context.compiler};
    return true;
}

struct CommandListLayout {
    std::size_t active;
    std::size_t mdeList;
    std::size_t allocator;
    std::size_t mdeAllocator;
};

} // namespace yaagl::native

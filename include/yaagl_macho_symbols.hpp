#pragma once

#include <mach-o/loader.h>
#include <mach-o/nlist.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <vector>

namespace yaagl::macho {

// A read-only view of an image already mapped by dyld. Local (non-exported)
// D3DMetal functions are in LC_SYMTAB, so dlsym alone cannot find them. Never
// scan instructions or fall back to a remembered address. __LINKEDIT file
// offsets and virtual addresses are deliberately kept in different domains.
class Symbols final {
public:
    explicit Symbols(const mach_header_64* image) : image_(image) { inspect(); }

    bool valid() const noexcept { return error_ == nullptr; }
    const char* error() const noexcept { return error_; }
    const std::array<std::uint8_t, 16>& uuid() const noexcept { return uuid_; }
    const void* image() const noexcept { return image_; }

    void* find(const char* name, bool executable, const char** reason = nullptr) const noexcept {
        const auto fail = [reason](const char* message) -> void* {
            if (reason) *reason = message;
            return nullptr;
        };
        if (reason) *reason = nullptr;
        if (!valid()) return fail(error_);
        if (!name || !*name) return fail("empty-symbol-name");
        void* result = nullptr;
        for (std::uint32_t index = 0; index != symbolCount_; ++index) {
            const auto symbol = read<nlist_64>(symbols_ + index * sizeof(nlist_64));
            if ((symbol.n_type & N_STAB) || (symbol.n_type & N_TYPE) != N_SECT) continue;
            if (!symbol.n_un.n_strx) continue;
            if (symbol.n_un.n_strx >= stringBytes_) return fail("symbol-name-out-of-range");
            const char* candidate = strings_ + symbol.n_un.n_strx;
            const auto length = stringBytes_ - symbol.n_un.n_strx;
            if (!std::memchr(candidate, 0, length)) return fail("unterminated-symbol-name");
            if (std::strcmp(candidate, name) != 0) continue;
            if (!symbol.n_sect || symbol.n_sect > sections_.size())
                return fail("symbol-section-out-of-range");
            const Section& section = sections_[symbol.n_sect - 1];
            if (!contains(section.address, section.size, symbol.n_value, 1))
                return fail("symbol-value-out-of-section");
            if (executable && !section.executable) return fail("symbol-is-not-code");
            void* address = reinterpret_cast<void*>(mapped(symbol.n_value));
            if (!address) return fail("symbol-address-overflow");
            if (result && result != address) return fail("ambiguous-symbol");
            result = address;
        }
        return result ? result : fail("symbol-not-found");
    }

private:
    struct Section {
        std::uint64_t address;
        std::uint64_t size;
        bool executable;
    };

    template<class T> static T read(const void* pointer) noexcept {
        T value{};
        std::memcpy(&value, pointer, sizeof(value));
        return value;
    }

    static bool contains(std::uint64_t base, std::uint64_t size,
                         std::uint64_t address, std::uint64_t length) noexcept {
        return address >= base && address - base <= size && length <= size - (address - base);
    }

    std::uintptr_t mapped(std::uint64_t address) const noexcept {
        const auto base = reinterpret_cast<std::uintptr_t>(image_);
        if (address < textAddress_ || address - textAddress_ >
            std::numeric_limits<std::uintptr_t>::max() - base) return 0;
        return base + static_cast<std::uintptr_t>(address - textAddress_);
    }

    void inspect() {
        error_ = "invalid-mach-o-header";
        if (!image_) return;
        const auto header = read<mach_header_64>(image_);
        if (header.magic != MH_MAGIC_64 || header.filetype != MH_DYLIB ||
            header.ncmds > header.sizeofcmds / sizeof(load_command)) return;
        const auto* commands = reinterpret_cast<const std::uint8_t*>(image_) + sizeof(header);
        std::size_t position = 0;
        bool haveText = false, haveLinkedit = false, haveSymtab = false, haveUuid = false;
        segment_command_64 linkedit{};
        symtab_command table{};
        for (std::uint32_t index = 0; index != header.ncmds; ++index) {
            error_ = "invalid-load-command-range";
            if (!contains(0, header.sizeofcmds, position, sizeof(load_command))) return;
            const auto command = read<load_command>(commands + position);
            if (command.cmdsize < sizeof(command) || command.cmdsize % alignof(std::uint64_t) ||
                !contains(0, header.sizeofcmds, position, command.cmdsize)) return;
            if (command.cmd == LC_SEGMENT_64) {
                if (command.cmdsize < sizeof(segment_command_64)) return;
                const auto segment = read<segment_command_64>(commands + position);
                if (segment.nsects > (command.cmdsize - sizeof(segment)) / sizeof(section_64) ||
                    segment.filesize > segment.vmsize) return;
                if (std::strncmp(segment.segname, SEG_TEXT, sizeof(segment.segname)) == 0) {
                    if (haveText || segment.fileoff != 0 || segment.filesize <
                        sizeof(header) + static_cast<std::uint64_t>(header.sizeofcmds)) return;
                    textAddress_ = segment.vmaddr;
                    haveText = true;
                }
                if (std::strncmp(segment.segname, SEG_LINKEDIT, sizeof(segment.segname)) == 0) {
                    if (haveLinkedit) return;
                    linkedit = segment;
                    haveLinkedit = true;
                }
                for (std::uint32_t sectionIndex = 0; sectionIndex != segment.nsects; ++sectionIndex) {
                    const auto section = read<section_64>(commands + position + sizeof(segment) +
                        sectionIndex * sizeof(section_64));
                    if (!contains(segment.vmaddr, segment.vmsize, section.addr, section.size)) return;
                    const bool code = (segment.initprot & VM_PROT_EXECUTE) &&
                        (section.flags & (S_ATTR_PURE_INSTRUCTIONS | S_ATTR_SOME_INSTRUCTIONS));
                    sections_.push_back({section.addr, section.size, code});
                }
            } else if (command.cmd == LC_SYMTAB) {
                if (haveSymtab || command.cmdsize != sizeof(symtab_command)) return;
                table = read<symtab_command>(commands + position);
                haveSymtab = true;
            } else if (command.cmd == LC_UUID) {
                if (haveUuid || command.cmdsize != sizeof(uuid_command)) return;
                const auto uuid = read<uuid_command>(commands + position);
                std::memcpy(uuid_.data(), uuid.uuid, uuid_.size());
                haveUuid = true;
            }
            position += command.cmdsize;
        }
        error_ = "missing-native-symbol-table";
        if (!haveText || !haveLinkedit || !haveSymtab || !haveUuid ||
            position != header.sizeofcmds) return;
        error_ = "invalid-linkedit-range";
        if (!contains(linkedit.fileoff, linkedit.filesize, table.symoff,
                      std::uint64_t(table.nsyms) * sizeof(nlist_64)) ||
            !contains(linkedit.fileoff, linkedit.filesize, table.stroff, table.strsize)) return;
        const auto linkeditBase = mapped(linkedit.vmaddr);
        if (!linkeditBase || linkedit.vmsize >
            std::numeric_limits<std::uintptr_t>::max() - linkeditBase) return;
        symbols_ = reinterpret_cast<const std::uint8_t*>(linkeditBase +
            (std::uint64_t(table.symoff) - linkedit.fileoff));
        strings_ = reinterpret_cast<const char*>(linkeditBase +
            (std::uint64_t(table.stroff) - linkedit.fileoff));
        symbolCount_ = table.nsyms;
        stringBytes_ = table.strsize;
        error_ = nullptr;
    }

    const mach_header_64* image_ = nullptr;
    const char* error_ = nullptr;
    std::uint64_t textAddress_ = 0;
    std::array<std::uint8_t, 16> uuid_{};
    std::vector<Section> sections_;
    const std::uint8_t* symbols_ = nullptr;
    const char* strings_ = nullptr;
    std::uint32_t symbolCount_ = 0;
    std::uint32_t stringBytes_ = 0;
};

} // namespace yaagl::macho

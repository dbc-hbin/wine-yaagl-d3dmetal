import CryptoKit
import Darwin
import Foundation

private struct Asset: Decodable {
    let name: String
    let sha256: String
}

private struct Release: Decodable {
    let version: String
    let dmg: Asset
    let evaluationDmg: Asset
    let license: Asset
    let acknowledgements: Asset
    let frameworkPath: String
}

private struct Hashes: Decodable {
    let pristineD3DMetal: String
    let pristineConverter: String
    let compositePreSign: String
    let compositePayload: String
    let rawSidecar: String
    let signedSidecar: String
}

private struct Patch: Decodable {
    let offset: Int
    let bytes: String
}

private struct Patches: Decodable {
    let nativeSidecar: [Patch]
}

private struct Recipe: Decodable {
    let schema: Int
    let release: Release
    let frameworkResources: [String: String]
    let hashes: Hashes
    let patches: Patches
}

private struct Options {
    var output: URL?
    var dmg: URL?
    var framework: URL?
    var license: URL?
    var acknowledgements: URL?
    var sidecarModule: URL?
    var acceptAppleLicense = false
    var force = false
}

private enum PreparationError: Error, CustomStringConvertible {
    case message(String)
    var description: String {
        switch self { case .message(let text): return text }
    }
}

private func fail(_ message: String) throws -> Never { throw PreparationError.message(message) }
private let files = FileManager.default
private let dependency = "@loader_path/Resources/libD3DMetalSidecar.dylib"

private func path(_ value: String) -> URL {
    URL(fileURLWithPath: value, relativeTo: URL(fileURLWithPath: files.currentDirectoryPath, isDirectory: true)).standardizedFileURL
}

private func executableDirectory() throws -> URL {
    var length: UInt32 = 0
    _ = _NSGetExecutablePath(nil, &length)
    guard length > 0 else { try fail("could not determine executable path length") }
    var buffer = [CChar](repeating: 0, count: Int(length))
    let status = buffer.withUnsafeMutableBufferPointer { _NSGetExecutablePath($0.baseAddress, &length) }
    guard status == 0 else { try fail("could not determine executable path") }
    return URL(fileURLWithPath: String(cString: buffer)).resolvingSymlinksInPath().deletingLastPathComponent()
}

private func parseArguments() throws -> Options? {
    var options = Options()
    let arguments = Array(CommandLine.arguments.dropFirst())
    var index = 0
    while index < arguments.count {
        let argument = arguments[index]
        if argument == "--help" {
            print("Usage: prepare-d3dmetal-runtime --output DIR --accept-apple-license (--dmg GPTK_BETA3.dmg | --framework D3DMetal.framework --license License.rtf --acknowledgements Acknowledgements.rtf) [--sidecar-module DYLIB] [--force]")
            return nil
        }
        if argument == "--accept-apple-license" { options.acceptAppleLicense = true }
        else if argument == "--force" { options.force = true }
        else {
            guard ["--output", "--dmg", "--framework", "--license", "--acknowledgements", "--sidecar-module"].contains(argument) else {
                try fail("unknown argument: \(argument)")
            }
            index += 1
            guard index < arguments.count else { try fail("missing value for \(argument)") }
            let value = path(arguments[index])
            switch argument {
            case "--output": options.output = value
            case "--dmg": options.dmg = value
            case "--framework": options.framework = value
            case "--license": options.license = value
            case "--acknowledgements": options.acknowledgements = value
            default: options.sidecarModule = value
            }
        }
        index += 1
    }
    guard options.output != nil else { try fail("--output DIR is required") }
    guard options.acceptAppleLicense else {
        try fail("refusing to prepare Apple software without --accept-apple-license; review the pinned License.rtf first")
    }
    guard (options.dmg != nil) != (options.framework != nil) else {
        try fail("exactly one of --dmg or --framework is required; no downloads are performed")
    }
    if options.framework != nil {
        guard options.license != nil, options.acknowledgements != nil else {
            try fail("--framework requires the beta3 --license and --acknowledgements files")
        }
    } else if options.license != nil || options.acknowledgements != nil {
        try fail("--dmg uses license documents from the image; do not supply overrides")
    }
    return options
}

private func regularFile(_ url: URL) throws {
    let attributes = try files.attributesOfItem(atPath: url.path)
    guard attributes[.type] as? FileAttributeType == .typeRegular else { try fail("expected regular file: \(url.path)") }
}

private func exists(_ url: URL) -> Bool {
    (try? files.attributesOfItem(atPath: url.path)) != nil
}

private func hash(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

private func hashFile(_ url: URL) throws -> String {
    try regularFile(url)
    let handle = try FileHandle(forReadingFrom: url)
    defer { try? handle.close() }
    var sha = SHA256()
    while let chunk = try handle.read(upToCount: 1024 * 1024), !chunk.isEmpty {
        sha.update(data: chunk)
    }
    return sha.finalize().map { String(format: "%02x", $0) }.joined()
}

@discardableResult
private func expectHash(_ url: URL, _ expected: String, _ description: String) throws -> String {
    let actual = try hashFile(url)
    guard actual == expected else { try fail("\(description) hash mismatch: expected \(expected), got \(actual)") }
    return actual
}

// Captured output goes to regular files rather than pipes.
@discardableResult
private func run(_ executable: String, _ arguments: [String], capture: Bool = false) throws -> String {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = arguments
    let stdoutURL = files.temporaryDirectory.appendingPathComponent("d3dmetal-stdout-\(UUID().uuidString)")
    let stderrURL = files.temporaryDirectory.appendingPathComponent("d3dmetal-stderr-\(UUID().uuidString)")
    var stdout: FileHandle?
    var stderr: FileHandle?
    if capture {
        files.createFile(atPath: stdoutURL.path, contents: nil)
        files.createFile(atPath: stderrURL.path, contents: nil)
        stdout = try FileHandle(forWritingTo: stdoutURL)
        stderr = try FileHandle(forWritingTo: stderrURL)
        process.standardOutput = stdout
        process.standardError = stderr
    } else {
        process.standardOutput = FileHandle.standardOutput
        process.standardError = FileHandle.standardError
    }
    defer {
        try? stdout?.close()
        try? stderr?.close()
        if capture {
            try? files.removeItem(at: stdoutURL)
            try? files.removeItem(at: stderrURL)
        }
    }
    try process.run()
    process.waitUntilExit()
    try stdout?.close()
    try stderr?.close()
    stdout = nil
    stderr = nil
    let result: String
    if capture {
        let out = try Data(contentsOf: stdoutURL)
        let err = try Data(contentsOf: stderrURL)
        guard out.count + err.count <= 1024 * 1024 else { try fail("oversized output from \(executable)") }
        result = String(decoding: out + err, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    } else { result = "" }
    guard process.terminationStatus == 0 else {
        try fail("\(executable) exited \(process.terminationStatus)\(result.isEmpty ? "" : ": \(result)")")
    }
    return result
}

private func symlink(_ url: URL, target: String) throws {
    let attributes = try files.attributesOfItem(atPath: url.path)
    guard attributes[.type] as? FileAttributeType == .typeSymbolicLink else { try fail("expected symlink: \(url.path)") }
    let actual = try files.destinationOfSymbolicLink(atPath: url.path)
    guard actual == target else { try fail("unexpected symlink target for \(url.path): \(actual)") }
}

private func frameworkLinks(_ framework: URL) throws {
    try symlink(framework.appendingPathComponent("D3DMetal"), target: "Versions/Current/D3DMetal")
    try symlink(framework.appendingPathComponent("Resources"), target: "Versions/Current/Resources")
    try symlink(framework.appendingPathComponent("Versions/Current"), target: "A")
}

private func replacePreservingMode(_ url: URL, data: Data) throws {
    let attributes = try files.attributesOfItem(atPath: url.path)
    let temporary = url.deletingLastPathComponent().appendingPathComponent(".patch-\(UUID().uuidString)")
    defer { try? files.removeItem(at: temporary) }
    try data.write(to: temporary, options: .withoutOverwriting)
    try files.setAttributes([.posixPermissions: attributes[.posixPermissions] ?? 0o755], ofItemAtPath: temporary.path)
    // rename(2) replaces a regular file in the same directory atomically.
    guard rename(temporary.path, url.path) == 0 else { try fail("cannot replace \(url.path): \(String(cString: strerror(errno)))") }
}

private func patch(_ url: URL, sites: [Patch], input: String, output: String, label: String) throws {
    try expectHash(url, input, "\(label) input")
    var data = try Data(contentsOf: url)
    try data.withUnsafeMutableBytes { raw in
        guard let base = raw.baseAddress else { try fail("empty \(label) input") }
        for site in sites {
            guard let bytes = Data(base64Encoded: site.bytes), !bytes.isEmpty,
                  site.offset >= 0, site.offset <= raw.count, bytes.count <= raw.count - site.offset else {
                try fail("invalid \(label) patch site")
            }
            bytes.withUnsafeBytes { source in
                base.advanced(by: site.offset).copyMemory(from: source.baseAddress!, byteCount: bytes.count)
            }
        }
    }
    guard hash(data) == output else { try fail("\(label) patch result hash mismatch: expected \(output), got \(hash(data))") }
    try replacePreservingMode(url, data: data)
    try expectHash(url, output, "\(label) written result")
}

private func unsigned(_ bytes: Data, _ offset: Int) throws -> Int {
    guard offset >= 0, offset <= bytes.count - 4 else { try fail("truncated Mach-O load command") }
    return bytes[offset..<offset + 4].enumerated().reduce(0) { $0 | (Int($1.element) << ($1.offset * 8)) }
}

private func inspectPayload(_ url: URL, expected: String) throws {
    let bytes = try Data(contentsOf: url)
    guard bytes.count >= 32, try unsigned(bytes, 0) == 0xfeedfacf,
          try unsigned(bytes, 4) == 0x01000007, try unsigned(bytes, 12) == 6 else {
        try fail("unsupported final Mach-O binary")
    }
    let count = try unsigned(bytes, 16)
    let commandsSize = try unsigned(bytes, 20)
    guard commandsSize <= bytes.count - 32, count <= commandsSize / 8 else { try fail("invalid Mach-O command table") }
    let end = 32 + commandsSize
    var cursor = 32
    var dependencies = 0
    var signature: (command: Int, data: Int)?
    var linkEdit: Int?
    for _ in 0..<count {
        guard cursor <= end - 8 else { try fail("truncated Mach-O command") }
        let command = try unsigned(bytes, cursor)
        let size = try unsigned(bytes, cursor + 4)
        guard size >= 8, size <= end - cursor else { try fail("invalid Mach-O command length") }
        if command == 0x19 {
            guard size >= 72 else { try fail("invalid LC_SEGMENT_64") }
            let name = bytes[cursor + 8..<cursor + 24].prefix { $0 != 0 }
            if String(data: Data(name), encoding: .utf8) == "__LINKEDIT" {
                guard linkEdit == nil else { try fail("duplicate __LINKEDIT") }
                linkEdit = cursor
            }
        }
        if command == 0xc {
            guard size >= 24 else { try fail("invalid LC_LOAD_DYLIB") }
            let offset = try unsigned(bytes, cursor + 8)
            guard offset >= 24, offset < size,
                  let terminator = bytes[cursor + offset..<cursor + size].firstIndex(of: 0) else {
                try fail("invalid LC_LOAD_DYLIB name")
            }
            if String(data: bytes[cursor + offset..<terminator], encoding: .utf8) == dependency { dependencies += 1 }
        }
        if command == 0x1d {
            guard size == 16, signature == nil else { try fail("invalid LC_CODE_SIGNATURE") }
            let offset = try unsigned(bytes, cursor + 8)
            let length = try unsigned(bytes, cursor + 12)
            guard offset >= end, offset <= bytes.count, length > 0, length == bytes.count - offset else {
                try fail("invalid code signature range")
            }
            signature = (cursor, offset)
        }
        cursor += size
    }
    guard cursor == end, dependencies == 1, let signature, let linkEdit else {
        try fail("final D3DMetal does not contain exactly one native sidecar dependency and valid signature")
    }
    var normalized = Data(bytes.prefix(signature.data))
    normalized.resetBytes(in: signature.command + 8..<signature.command + 16)
    normalized.resetBytes(in: linkEdit + 32..<linkEdit + 40)
    normalized.resetBytes(in: linkEdit + 48..<linkEdit + 56)
    let actual = hash(normalized)
    guard actual == expected else { try fail("composite payload hash mismatch: expected \(expected), got \(actual)") }
}

private func adhoc(_ url: URL) throws {
    let output = try run("/usr/bin/codesign", ["-d", "--verbose=4", url.path], capture: true)
    guard output.contains("Signature=adhoc") else { try fail("not ad-hoc signed: \(url.path)") }
}

private func publish(_ result: URL, output: URL, force: Bool) throws {
    guard exists(output) else { try files.moveItem(at: result, to: output); return }
    guard force else { try fail("output already exists: \(output.path); pass --force to replace it") }
    // On APFS the swap is one namespace operation: a failure cannot remove the old output.
    // The former output is left at result and removed with the staging directory.
    guard renameatx_np(AT_FDCWD, result.path, AT_FDCWD, output.path, UInt32(RENAME_SWAP)) == 0 else {
        try fail("cannot atomically replace \(output.path): \(String(cString: strerror(errno)))")
    }
}

private func prepare(_ options: Options, recipe: Recipe) throws {
    guard let output = options.output else { try fail("missing output") }
    if exists(output), !options.force { try fail("output already exists: \(output.path); pass --force to replace it") }
    let release = recipe.release
    let hashes = recipe.hashes
    for input in [options.dmg, options.framework, options.license, options.acknowledgements, options.sidecarModule].compactMap({ $0 }) {
        let resolved = input.resolvingSymlinksInPath().path
        let destination = output.resolvingSymlinksInPath().path
        guard resolved != destination, !resolved.hasPrefix(destination + "/"),
              !destination.hasPrefix(resolved + "/") else {
            try fail("input and output must not contain one another: \(input.path)")
        }
    }
    let sidecar = try options.sidecarModule ?? executableDirectory().appendingPathComponent("libD3DMetalSidecar.dylib")
    let sourceHash = try hashFile(sidecar)
    guard sourceHash == hashes.rawSidecar || sourceHash == hashes.signedSidecar else {
        try fail("unexpected native sidecar module: \(sourceHash)")
    }
    try files.createDirectory(at: output.deletingLastPathComponent(), withIntermediateDirectories: true)
    let temporary = output.deletingLastPathComponent().appendingPathComponent(".d3dmetal-prepare-\(UUID().uuidString)", isDirectory: true)
    let result = temporary.appendingPathComponent("result", isDirectory: true)
    let framework = result.appendingPathComponent("D3DMetal.framework", isDirectory: true)
    try files.createDirectory(at: result, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    defer { try? files.removeItem(at: temporary) }
    var mounts: [URL] = []
    defer {
        for mount in mounts.reversed() {
            do { try run("/usr/bin/hdiutil", ["detach", mount.path], capture: true) }
            catch { fputs("prepare-d3dmetal-runtime: could not detach \(mount.path): \(error)\n", stderr) }
        }
    }
    func mount(_ image: URL, name: String) throws -> URL {
        let destination = temporary.appendingPathComponent(name, isDirectory: true)
        try files.createDirectory(at: destination, withIntermediateDirectories: false)
        try run("/usr/bin/hdiutil", ["attach", "-readonly", "-nobrowse", "-mountpoint", destination.path, image.path], capture: true)
        mounts.append(destination)
        return destination
    }
    let source: URL
    let license: URL
    let acknowledgements: URL
    var sourceMetadata: [String: Any] = ["kind": "local-framework", "version": release.version]
    if let image = options.dmg {
        let imageHash = try hashFile(image)
        let evaluation: URL
        if imageHash == release.dmg.sha256 {
            let outer = try mount(image, name: "toolkit")
            let nested = outer.appendingPathComponent(release.evaluationDmg.name)
            try expectHash(nested, release.evaluationDmg.sha256, "beta3 evaluation image")
            evaluation = try mount(nested, name: "evaluation")
        } else if imageHash == release.evaluationDmg.sha256 {
            evaluation = try mount(image, name: "evaluation")
        } else { try fail("input is not the pinned Apple GPTK beta3 image: \(imageHash)") }
        source = evaluation.appendingPathComponent(release.frameworkPath)
        license = evaluation.appendingPathComponent(release.license.name)
        acknowledgements = evaluation.appendingPathComponent(release.acknowledgements.name)
        sourceMetadata = ["kind": "local-apple-dmg", "version": release.version, "sha256": imageHash]
    } else {
        guard let local = options.framework, let localLicense = options.license,
              let localAcknowledgements = options.acknowledgements else { try fail("missing framework input") }
        source = local
        license = localLicense
        acknowledgements = localAcknowledgements
    }
    try expectHash(license, release.license.sha256, "beta3 license")
    try expectHash(acknowledgements, release.acknowledgements.sha256, "beta3 acknowledgements")
    try frameworkLinks(source)
    try run("/usr/bin/ditto", [source.path, framework.path])
    try frameworkLinks(framework)
    let resources = framework.appendingPathComponent("Versions/A/Resources", isDirectory: true)
    let binary = framework.appendingPathComponent("Versions/A/D3DMetal")
    let converter = resources.appendingPathComponent("libmetalirconverter.dylib")
    let info = resources.appendingPathComponent("Info.plist")
    try regularFile(binary)
    try regularFile(converter)
    try regularFile(info)
    let version = try run("/usr/libexec/PlistBuddy", ["-c", "Print :CFBundleShortVersionString", info.path], capture: true)
    guard version == release.version else { try fail("unexpected D3DMetal version: \(version)") }
    try expectHash(binary, hashes.pristineD3DMetal, "pristine D3DMetal")
    try expectHash(converter, hashes.pristineConverter, "pristine converter")
    for (name, expected) in recipe.frameworkResources {
        try expectHash(resources.appendingPathComponent(name), expected, "pristine beta3 resource \(name)")
    }
    try run("/usr/bin/codesign", ["--verify", "--deep", "--strict", framework.path])

    try patch(binary, sites: recipe.patches.nativeSidecar, input: hashes.pristineD3DMetal,
              output: hashes.compositePreSign, label: "native sidecar")
    try inspectPayload(binary, expected: hashes.compositePayload)
    let nested = resources.appendingPathComponent("libD3DMetalSidecar.dylib")
    try files.copyItem(at: sidecar, to: nested)
    try files.setAttributes([.posixPermissions: 0o755], ofItemAtPath: nested.path)
    try expectHash(nested, sourceHash, "copied native sidecar module")
    try run("/usr/bin/codesign", ["--force", "--sign", "-", nested.path])
    // Never deep-sign: the Apple converter must remain byte-for-byte pristine.
    try run("/usr/bin/codesign", ["--force", "--sign", "-", framework.path])
    for item in [nested, binary] {
        try run("/usr/bin/codesign", ["--verify", "--strict", item.path])
        try adhoc(item)
    }
    try run("/usr/bin/codesign", ["--verify", "--deep", "--strict", framework.path])
    let signedSidecarHash = try hashFile(nested)
    try expectHash(converter, hashes.pristineConverter, "final pristine converter")
    for (name, expected) in recipe.frameworkResources {
        try expectHash(resources.appendingPathComponent(name), expected, "final pristine beta3 resource \(name)")
    }
    let finalD3DMetalHash = try hashFile(binary)
    try inspectPayload(binary, expected: hashes.compositePayload)
    try frameworkLinks(framework)

    for (asset, name) in [(license, "License.rtf"), (acknowledgements, "Acknowledgements.rtf")] {
        try files.copyItem(at: asset, to: result.appendingPathComponent(name))
    }
    let manifest: [String: Any] = [
        "schema": 3,
        "source": sourceMetadata,
        "licenseSha256": release.license.sha256,
        "acknowledgementsSha256": release.acknowledgements.sha256,
        "licenseAcceptance": "explicit-cli-flag",
        "framework": ["version": release.version, "pristineD3DMetalSha256": hashes.pristineD3DMetal,
                      "compositeInputSha256": hashes.pristineD3DMetal, "compositePreSignSha256": hashes.compositePreSign,
                      "compositePayloadSha256": hashes.compositePayload, "finalD3DMetalSha256": finalD3DMetalHash,
                      "compositeMode": "patched-signed", "signature": "adhoc"],
        "metalIrConverter": ["pristineSha256": hashes.pristineConverter, "patchMode": "pristine"],
        "frameworkResources": recipe.frameworkResources,
        "nativeSidecar": ["sourceSha256": sourceHash, "signedSha256": signedSidecarHash,
                             "dependency": dependency, "signature": "adhoc"],
        "patchPipeline": ["native-sidecar-patch", "native-sidecar-module", "final-framework-reseal"]
    ]
    let json = try JSONSerialization.data(withJSONObject: manifest, options: [.prettyPrinted, .sortedKeys])
    try (json + Data([10])).write(to: result.appendingPathComponent("prepared-d3dmetal.json"), options: .withoutOverwriting)
    try publish(result, output: output, force: options.force)
    print("Prepared D3DMetal.framework at \(output.path)")
}

@main
private enum Main {
    static func main() {
        do {
            if let options = try parseArguments() {
                guard let data = Data(base64Encoded: AutopatchRecipe.base64) else { try fail("invalid embedded recipe encoding") }
                let recipe = try JSONDecoder().decode(Recipe.self, from: data)
                guard recipe.schema == 2 else { try fail("unsupported embedded recipe schema") }
                try prepare(options, recipe: recipe)
            }
        } catch {
            fputs("prepare-d3dmetal-runtime: \(error)\n", stderr)
            exit(1)
        }
    }
}

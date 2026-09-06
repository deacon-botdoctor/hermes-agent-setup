#!/usr/bin/env swift

import CryptoKit
import FileProvider
import Foundation

private struct Options {
    let path: String
    let allowedRoot: String
    let apply: Bool
    let confirmationToken: String?
}

private struct ProviderState {
    let uploaded: Bool
    let uploading: Bool
    let syncPaused: Bool
    let keepDownloaded: Bool
    let downloaded: Bool
    let makeOnlineOnlyAvailable: Bool
    let raw: String
}

private enum ToolError: Error, CustomStringConvertible {
    case usage(String)
    case validation(String)
    case provider(String)

    var description: String {
        switch self {
        case .usage(let message), .validation(let message), .provider(let message):
            return message
        }
    }
}

private func parseOptions() throws -> Options {
    var path: String?
    var allowedRoot: String?
    var apply = false
    var confirmationToken: String?
    var index = 1
    while index < CommandLine.arguments.count {
        let argument = CommandLine.arguments[index]
        switch argument {
        case "--path", "--allowed-root", "--confirmation-token":
            guard index + 1 < CommandLine.arguments.count else {
                throw ToolError.usage("missing value for \(argument)")
            }
            let value = CommandLine.arguments[index + 1]
            if argument == "--path" {
                path = value
            } else if argument == "--allowed-root" {
                allowedRoot = value
            } else {
                confirmationToken = value
            }
            index += 2
        case "--apply":
            apply = true
            index += 1
        case "--help", "-h":
            throw ToolError.usage(
                "usage: hermes-fileprovider-online-only.swift --path PATH " +
                "[--allowed-root ROOT] [--apply --confirmation-token TOKEN]"
            )
        default:
            throw ToolError.usage("unknown argument: \(argument)")
        }
    }
    guard let path else {
        throw ToolError.usage("--path is required")
    }
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    return Options(
        path: path,
        allowedRoot: allowedRoot ?? home + "/Library/CloudStorage/Dropbox",
        apply: apply,
        confirmationToken: confirmationToken
    )
}

private func canonicalURL(_ raw: String) throws -> URL {
    let expanded = NSString(string: raw).expandingTildeInPath
    let url = URL(fileURLWithPath: expanded).standardizedFileURL
    guard FileManager.default.fileExists(atPath: url.path) else {
        throw ToolError.validation("path does not exist: \(url.path)")
    }
    let values = try url.resourceValues(forKeys: [.isSymbolicLinkKey])
    guard values.isSymbolicLink != true else {
        throw ToolError.validation("symbolic-link targets are not allowed")
    }
    return url.resolvingSymlinksInPath().standardizedFileURL
}

private func validateTarget(path: URL, allowedRoot: URL) throws {
    let root = allowedRoot.path.hasSuffix("/") ? allowedRoot.path : allowedRoot.path + "/"
    guard path.path.hasPrefix(root), path.path != allowedRoot.path else {
        throw ToolError.validation("target must be a child of the exact allowed root")
    }
    let lowered = path.path.lowercased()
    let protectedFragments = ["/.hermes/", "/mackey brain/", "/documents/codex/"]
    guard protectedFragments.allSatisfy({ !lowered.contains($0) }) else {
        throw ToolError.validation("target intersects a protected runtime or working-data path")
    }
}

private func processOutput(_ executable: String, _ arguments: [String]) throws -> String {
    let process = Process()
    let pipe = Pipe()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = arguments
    process.standardOutput = pipe
    process.standardError = pipe
    try process.run()
    process.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    let output = String(data: data, encoding: .utf8) ?? ""
    guard process.terminationStatus == 0 else {
        throw ToolError.provider("fileproviderctl evaluate failed: \(output.prefix(500))")
    }
    return output
}

private func firstFlag(_ name: String, in text: String) throws -> Bool {
    let escaped = NSRegularExpression.escapedPattern(for: name)
    let regex = try NSRegularExpression(pattern: "\\b\(escaped)\\s*=\\s*([01]);")
    let range = NSRange(text.startIndex..<text.endIndex, in: text)
    guard let match = regex.firstMatch(in: text, range: range),
          let valueRange = Range(match.range(at: 1), in: text) else {
        throw ToolError.provider("provider state did not expose \(name)")
    }
    return text[valueRange] == "1"
}

private func providerState(for path: URL) throws -> ProviderState {
    let output = try processOutput("/usr/bin/fileproviderctl", ["evaluate", path.path])
    return ProviderState(
        uploaded: try firstFlag("isUploaded", in: output),
        uploading: try firstFlag("isUploading", in: output),
        syncPaused: try firstFlag("isSyncPaused", in: output),
        keepDownloaded: try firstFlag("isKeepDownloaded", in: output),
        downloaded: try firstFlag("isDownloaded", in: output),
        makeOnlineOnlyAvailable: output.range(
            of: #"com\.getdropbox\.dropbox\.fileprovider\.action\.make_online_only:.*- YES"#,
            options: .regularExpression
        ) != nil,
        raw: output
    )
}

private func resolveProviderItem(for path: URL) throws -> (NSFileProviderItemIdentifier, NSFileProviderDomainIdentifier) {
    let semaphore = DispatchSemaphore(value: 0)
    var result: (NSFileProviderItemIdentifier, NSFileProviderDomainIdentifier)?
    var capturedError: Error?
    NSFileProviderManager.getIdentifierForUserVisibleFile(at: path) { identifier, domain, error in
        if let identifier, let domain {
            result = (identifier, domain)
        } else {
            capturedError = error ?? ToolError.provider("provider identifier or domain missing")
        }
        semaphore.signal()
    }
    semaphore.wait()
    if let capturedError {
        throw capturedError
    }
    guard let result else {
        throw ToolError.provider("provider identifier resolution failed")
    }
    return result
}

private func evict(path: URL) throws {
    // FileManager is the public consumer-side lane for user-visible files.
    // NSFileProviderManager.evictItem is provider-app scoped on macOS and
    // rejects an otherwise valid standalone operator tool.
    try FileManager.default.evictUbiquitousItem(at: path)
}

private func freeBytes(at path: URL) -> UInt64? {
    guard let attributes = try? FileManager.default.attributesOfFileSystem(forPath: path.path),
          let value = attributes[.systemFreeSize] as? NSNumber else {
        return nil
    }
    return value.uint64Value
}

private func token(path: URL, domain: NSFileProviderDomainIdentifier, item: NSFileProviderItemIdentifier) -> String {
    let value = path.path + "\n" + domain.rawValue + "\n" + item.rawValue
    return SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
}

private func emit(_ payload: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
          let text = String(data: data, encoding: .utf8) else {
        fputs("unable to encode JSON result\n", stderr)
        return
    }
    print(text)
}

do {
    let options = try parseOptions()
    let path = try canonicalURL(options.path)
    let allowedRoot = try canonicalURL(options.allowedRoot)
    try validateTarget(path: path, allowedRoot: allowedRoot)
    let stateBefore = try providerState(for: path)
    guard stateBefore.uploaded else {
        throw ToolError.validation("provider reports that the target is not fully uploaded")
    }
    guard !stateBefore.uploading else {
        throw ToolError.validation("provider reports an upload in progress")
    }
    guard !stateBefore.syncPaused else {
        throw ToolError.validation("provider sync is paused")
    }
    guard !stateBefore.keepDownloaded else {
        throw ToolError.validation("provider marks this target as keep-downloaded")
    }
    guard stateBefore.makeOnlineOnlyAvailable else {
        throw ToolError.validation("provider does not offer make-online-only for this target")
    }
    let (identifier, domainIdentifier) = try resolveProviderItem(for: path)
    let confirmation = token(path: path, domain: domainIdentifier, item: identifier)
    let before = freeBytes(at: path)
    if !options.apply {
        emit([
            "ok": true,
            "mode": "dry-run",
            "path": path.path,
            "allowed_root": allowedRoot.path,
            "provider_domain": domainIdentifier.rawValue,
            "provider_item": identifier.rawValue,
            "confirmation_token": confirmation,
            "is_uploaded": stateBefore.uploaded,
            "is_uploading": stateBefore.uploading,
            "is_sync_paused": stateBefore.syncPaused,
            "is_keep_downloaded": stateBefore.keepDownloaded,
            "is_downloaded": stateBefore.downloaded,
            "free_before_bytes": before as Any,
        ])
        exit(0)
    }
    guard options.confirmationToken == confirmation else {
        throw ToolError.validation("--apply requires the exact confirmation token from a fresh dry-run")
    }
    try evict(path: path)
    let stateAfter = try providerState(for: path)
    let after = freeBytes(at: path)
    emit([
        "ok": true,
        "mode": "apply",
        "path": path.path,
        "provider_domain": domainIdentifier.rawValue,
        "provider_item": identifier.rawValue,
        "is_downloaded_before": stateBefore.downloaded,
        "is_downloaded_after": stateAfter.downloaded,
        "free_before_bytes": before as Any,
        "free_after_bytes": after as Any,
        "remote_data_deleted": false,
    ])
} catch {
    emit(["ok": false, "error": String(describing: error)])
    exit(1)
}

import AppKit
import Foundation

final class PathLinkDelegate: NSObject, NSApplicationDelegate {
    private var pending = 0
    private var launched = false
    private var idleExit: DispatchWorkItem?
    private var failures: [String] = []
    private var showingAlert = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        launched = true
        finishIfIdle()
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        idleExit?.cancel()
        for url in urls {
            pending += 1
            guard let launcher = Bundle.main.object(forInfoDictionaryKey: "RexOMPLauncher") as? String,
                  launcher.hasPrefix("/"),
                  FileManager.default.isExecutableFile(atPath: launcher) else {
                completed("The Rex path opener is unavailable. Reinstall rex-omp and try again.")
                continue
            }
            let address = url.absoluteString
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                let errors = Pipe()
                process.executableURL = URL(fileURLWithPath: launcher)
                process.arguments = ["open-link", address]
                process.standardInput = FileHandle.nullDevice
                process.standardOutput = FileHandle.nullDevice
                process.standardError = errors
                var failure: String?
                do {
                    try process.run()
                    try? errors.fileHandleForWriting.close()
                    // Drain while the process runs, so concurrent failures cannot
                    // block on a full pipe. Retain only the actual diagnostic tail.
                    var diagnostic = Data()
                    while true {
                        let chunk = errors.fileHandleForReading.readData(ofLength: 4096)
                        if chunk.isEmpty { break }
                        diagnostic.append(chunk)
                        if diagnostic.count > 8192 {
                            diagnostic.removeFirst(diagnostic.count - 8192)
                        }
                    }
                    process.waitUntilExit()
                    if process.terminationReason != .exit || process.terminationStatus != 0 {
                        let message = String(decoding: diagnostic, as: UTF8.self)
                            .trimmingCharacters(in: .whitespacesAndNewlines)
                        failure = message.isEmpty
                            ? "rex-omp exited with status \(process.terminationStatus)." : message
                    }
                } catch {
                    failure = error.localizedDescription
                }
                try? errors.fileHandleForReading.close()
                try? errors.fileHandleForWriting.close()
                let result = failure
                DispatchQueue.main.async { self.completed(result) }
            }
        }
    }

    private func completed(_ failure: String?) {
        pending -= 1
        if let failure = failure { failures.append(failure) }
        finishIfIdle()
    }

    private func finishIfIdle() {
        guard launched, !showingAlert else { return }
        if !failures.isEmpty {
            showingAlert = true
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = failures.removeFirst()
            alert.addButton(withTitle: "OK")
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
            showingAlert = false
            finishIfIdle()
            return
        }
        guard pending == 0 else { return }
        idleExit?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self = self, self.pending == 0, self.failures.isEmpty,
                  !self.showingAlert else { return }
            NSApp.terminate(nil)
        }
        idleExit = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2, execute: work)
    }
}

let application = NSApplication.shared
let delegate = PathLinkDelegate()
application.setActivationPolicy(.accessory)
application.delegate = delegate
application.run()

// Anonbar — native macOS status-bar front end for Anonproxy (AppKit, no bundle).
//
// Thin UI over the proven CLI: start/stop per-profile proxies, switch
// engagements (isolated vaults), create profiles with a real text-input sheet,
// copy client env, open /audit, verify coverage, close-out export+archive.
//
// Build:  scripts/build_anonbar.sh   → build/anonbar
// Run:    build/anonbar              (or: ln -sf $PWD/build/anonbar /usr/local/bin/anonbar)
//
// Env overrides: ANONPROXY_HOME=<repo root>, PYTHON_BIN=<interpreter>,
//                ANONPROXY_API_TOKEN is honoured for audit links.
import Cocoa

let ICON_ON = "🛡️●"
let ICON_OFF = "🛡️○"

// MARK: - helpers

func sh(_ args: [String], cwd: URL, timeout: TimeInterval = 180,
        extraEnv: [String: String]? = nil) -> (Int32, String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: args[0].hasPrefix("/") ? args[0] : "/usr/bin/env")
    p.arguments = args[0].hasPrefix("/") ? Array(args.dropFirst()) : args
    p.currentDirectoryURL = cwd
    var env = ProcessInfo.processInfo.environment
    if let e = extraEnv { env.merge(e) { _, new in new } }
    p.environment = env
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = pipe
    do { try p.run() } catch { return (-1, "\(error)") }
    let deadline = Date().addingTimeInterval(timeout)
    while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
    if p.isRunning { p.terminate(); return (-1, "timed out") }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    return (p.terminationStatus, String(data: data, encoding: .utf8) ?? "")
}

func isExec(_ path: String) -> Bool {
    FileManager.default.isExecutableFile(atPath: path)
}

func tcpOpen(_ port: Int) -> Bool {
    // blocking connect to 127.0.0.1 returns instantly either way
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = UInt16(port).bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    defer { close(fd) }
    let r = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    return r == 0
}

func logTailLines(_ path: String) -> String {
    guard let all = try? String(contentsOfFile: path, encoding: .utf8) else {
        return "(log empty)"
    }
    let t = all.split(separator: "\n").suffix(4)
        .joined(separator: "\n")
        .trimmingCharacters(in: .whitespacesAndNewlines)
    return t.isEmpty ? "(log empty)" : String(t.prefix(280))
}

func alertBox(_ title: String, _ message: String) {
    DispatchQueue.main.async {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = message
        a.runModal()
    }
}

// MARK: - model

struct Profile {
    var name: String
    var scopeTerms: [String]
    var detectors: [String]
    var port: Int
    var ephemeral: Bool
    var notes: String
}

func loadProfiles(_ dir: URL) -> [Profile] {
    guard let files = try? FileManager.default.contentsOfDirectory(
        at: dir, includingPropertiesForKeys: nil) else { return [] }
    var out: [Profile] = []
    for f in files.sorted(by: { $0.lastPathComponent < $1.lastPathComponent })
    where f.pathExtension == "json" {
        guard let data = try? Data(contentsOf: f),
              let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let name = o["name"] as? String else { continue }
        out.append(Profile(
            name: name,
            scopeTerms: o["scope_terms"] as? [String] ?? [],
            detectors: o["detectors"] as? [String] ?? [],
            port: o["port"] as? Int ?? 8099,
            ephemeral: o["ephemeral"] as? Bool ?? false,
            notes: o["notes"] as? String ?? ""))
    }
    return out
}

// MARK: - app delegate

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()

    // repo root resolution — prefers a LIVE repo, falls back to the SNAPSHOT
    // of the package bundled inside this app, so moving/deleting the repo
    // never breaks the menu bar:
    //   1. $ANONPROXY_HOME          (explicit override)
    //   2. walk up from this binary (works whenever the app lives in the repo)
    //   3. ~/.anonproxy/home        (pointer file written by installer / picker)
    //   4. compile-time source path (last resort)
    //   5. bundled snapshot         (Anonbar.app/Contents/Resources/anonproxy)
    lazy var anonDir: URL =
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".anonproxy")
    private(set) var homeSource = "unknown"
    lazy var home: URL = resolveHome()
    lazy var profilesDir: URL = anonDir.appendingPathComponent("profiles")
    lazy var logsDir: URL = anonDir.appendingPathComponent("logs")

    func looksLikeRepo(_ u: URL) -> Bool {
        FileManager.default.fileExists(
            atPath: u.appendingPathComponent("anonproxy/__init__.py").path)
    }

    func resolveHome() -> URL {
        func valid(_ s: String) -> URL? {
            let std = URL(fileURLWithPath: s, isDirectory: true).standardized
            return looksLikeRepo(std) ? std : nil
        }
        if let e = ProcessInfo.processInfo.environment["ANONPROXY_HOME"], let u = valid(e) {
            homeSource = "env"
            return u
        }
        if let exe = Bundle.main.executableURL {
            var dir = exe.deletingLastPathComponent().standardized
            while dir.path != "/" && dir.path != "." {
                if let u = valid(dir.path) {
                    homeSource = "walk-up"
                    return u
                }
                dir.deleteLastPathComponent()
            }
        }
        let ptr = anonDir.appendingPathComponent("home")
        if let raw = try? String(contentsOf: ptr, encoding: .utf8) {
            let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if !s.isEmpty, let u = valid(s) {
                homeSource = "pointer"
                return u
            }
        }
        if let u = valid(URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()      // scripts/
            .deletingLastPathComponent()      // repo/
            .path) {
            homeSource = "compile-time"
            return u
        }
        // standalone fallback: the package snapshot shipped inside this app
        if let res = Bundle.main.resourceURL, let u = valid(res.path) {
            homeSource = "bundled"
            return u
        }
        let panel = NSOpenPanel()
        panel.message = "Where is your Anonproxy repo? (folder containing 'anonproxy/')"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        if panel.runModal() == .OK, let u = panel.url, looksLikeRepo(u) {
            try? FileManager.default.createDirectory(at: anonDir,
                                                     withIntermediateDirectories: true)
            try? u.path.write(to: ptr, atomically: true, encoding: .utf8)
            homeSource = "picked"
            return u
        }
        return URL(fileURLWithPath: "/nonexistent-anonproxy-home")
    }
    // GUI-launched apps get a bare PATH (no pyenv/homebrew shims), so hunt for
    // a real interpreter ourselves; require >= 3.10 like the project does.
    lazy var pythonBin: String = {
        if let e = ProcessInfo.processInfo.environment["PYTHON_BIN"], isExec(e) {
            return e
        }
        let nsHome = NSHomeDirectory()
        let candidates = [
            nsHome + "/.pyenv/shims/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]
        for c in candidates where isExec(c) {
            let r = sh([c, "-V"], cwd: home, timeout: 10)
            if r.0 == 0, let minor = r.1.split(separator: ".").dropFirst().first,
               Int(minor.prefix(while: { $0.isNumber })) ?? 0 >= 10 {
                return c
            }
        }
        return "python3"   // last resort: PATH lookup
    }()

    var proc: Process?
    var childProfile: String?
    var setupInProgress = false

    lazy var venvPy: String =
        anonDir.appendingPathComponent("venv/bin/python").path

    func venvReady() -> Bool {
        FileManager.default.isExecutableFile(atPath: venvPy)
    }

    func requirementsPath() -> String? {
        let inRepo = home.appendingPathComponent("requirements.txt").path
        if FileManager.default.fileExists(atPath: inRepo) { return inRepo }
        if let res = Bundle.main.resourceURL?
            .appendingPathComponent("requirements.txt").path,
           FileManager.default.fileExists(atPath: res) { return res }
        return nil
    }

    /// First-run bootstrap: an isolated venv under ~/.anonproxy/venv with the
    /// project's dependencies, so end users never run pip by hand.
    func ensureVenv(done: @escaping (Bool, String) -> Void) {
        if venvReady() { done(true, ""); return }
        let havePython = FileManager.default.isExecutableFile(atPath: pythonBin)
            || pythonBin == "python3"
        guard havePython else { done(false, "no python>=3.10 found"); return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            try? FileManager.default.createDirectory(at: self.anonDir,
                                                     withIntermediateDirectories: true)
            let mk = sh([self.pythonBin, "-m", "venv", self.venvPy
                          .replacingOccurrences(of: "/bin/python", with: "")],
                        cwd: self.home, timeout: 300)
            guard mk.0 == 0 else { done(false, mk.1); return }
            guard let req = self.requirementsPath() else {
                done(true, "(no requirements found — skipped pip)")
                return
            }
            let pip = sh([self.venvPy, "-m", "pip", "install", "--quiet",
                          "-r", req], cwd: self.home, timeout: 900)
            done(pip.0 == 0, pip.1)
        }
    }
    var selected: String = "" {
        didSet { persistSelection() }
    }

    func selectionPath() -> URL {
        anonDir.appendingPathComponent("anonbar-selected")
    }

    func persistSelection() {
        try? FileManager.default.createDirectory(at: anonDir,
                                                 withIntermediateDirectories: true)
        try? selected.write(to: selectionPath(), atomically: true, encoding: .utf8)
    }

    // ------------------------------------------------------------ lifecycle
    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)
        try? FileManager.default.createDirectory(at: logsDir,
                                                 withIntermediateDirectories: true)
        selected = (try? String(contentsOf: selectionPath(), encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if selected.isEmpty || profile(named: selected) == nil {
            selected = loadProfiles(profilesDir).first?.name ?? ""
        }
        item.menu = menu
        menu.delegate = self
        menuNeedsUpdate(menu)
        Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.refreshTitle()
        }
        refreshTitle()
    }

    func applicationWillTerminate(_ note: Notification) { stopChild(notify: false) }

    // ------------------------------------------------------------- plumbing
    func cliAsync(_ sub: [String], done: @escaping (Int32, String) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let r = self.cliSync(sub)
            done(r.0, r.1)
        }
    }

    func cliSync(_ sub: [String]) -> (Int32, String) {
        // PYTHONPATH pins `import anonproxy` to this repo regardless of cwd
        let r = sh([pythonBin, "-m", "anonproxy"] + sub, cwd: home,
                   extraEnv: ["PYTHONPATH": home.path])
        if r.0 != 0 {
            return (r.0, r.1 +
                "\n---\npython: \(pythonBin)\ncwd: \(home.path)")
        }
        return r
    }

    func profiles() -> [Profile] { loadProfiles(profilesDir) }

    func profile(named name: String) -> Profile? {
        profiles().first { $0.name == name }
    }

    func runningChild() -> Bool {
        if let p = proc, p.isRunning { return true }
        return false
    }

    func refreshTitle() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.item.button?.title =
                self.setupInProgress ? "🛡️◌"
                : (self.runningChild() ? ICON_ON : ICON_OFF)
        }
    }

    // ---------------------------------------------------------------- child
    func startChild(_ name: String) {
        guard !runningChild() else { return }
        guard let prof = profile(named: name) else {
            alertBox("Anonproxy", "profile '\(name)' not found")
            return
        }
        if tcpOpen(prof.port) {
            alertBox("Port already in use",
                     "127.0.0.1:\(prof.port) is serving something else.\n" +
                     "Stop it or change the port in profile '\(name)'.")
            return
        }
        if !venvReady() && !setupInProgress {
            setupInProgress = true
            refreshTitle()
            ensureVenv { [weak self] ok, out in
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.setupInProgress = false
                    self.refreshTitle()
                    if ok { self.startChild(name) }
                    else {
                        alertBox("First-time setup failed",
                                 String(out.suffix(400)) +
                                 "\n\n(needs any Python >= 3.10 on the machine)")
                    }
                }
            }
            return
        }
        if setupInProgress { return }
        let logPath = logsDir.appendingPathComponent("anonbar-\(name).log").path
        FileManager.default.createFile(atPath: logPath, contents: nil)
        let logf = FileHandle(forWritingAtPath: logPath)

        var childEnv = ProcessInfo.processInfo.environment
        childEnv["PYTHONPATH"] = home.path
        let p = Process()
        p.executableURL = URL(fileURLWithPath: venvPy)
        p.arguments = ["-m", "anonproxy", "up", name]
        p.currentDirectoryURL = home
        p.environment = childEnv
        p.standardOutput = logf
        p.standardError = logf
        do { try p.run() } catch {
            alertBox("Failed to start", "\(error)")
            return
        }
        proc = p
        childProfile = name
        refreshTitle()
        let captured = p
        captured.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self, self.proc === process else { return }
                self.proc = nil
                let code = process.terminationStatus
                self.refreshTitle()
                if code != 0 {
                    alertBox("Anonproxy exited (code \(code))",
                             "engagement \(name)\n\n" + logTailLines(logPath))
                }
            }
        }
    }

    func stopChild(notify: Bool) {
        guard let p = proc, p.isRunning else {
            proc = nil; childProfile = nil; refreshTitle(); return
        }
        p.terminate()
        let deadline = Date().addingTimeInterval(5)
        while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        proc = nil
        childProfile = nil
        refreshTitle()
        if notify { alertBox("Anonproxy stopped", "") }
    }

    // ----------------------------------------------------------------- menu
    func menuNeedsUpdate(_ m: NSMenu) {
        m.removeAllItems()
        let profs = profiles()

        let state = NSMenuItem(title:
            runningChild()
                ? "● running: \(childProfile ?? "?")"
                : "○ stopped — selected: \(selected.isEmpty ? "(none)" : selected)",
            action: nil, keyEquivalent: "")
        state.isEnabled = false
        m.addItem(state)
        m.addItem(.separator())

        let ss = m.addItem(withTitle: runningChild() ? "Stop" : "Start",
                           action: #selector(toggleStart), keyEquivalent: "")
        ss.target = self
        if selected.isEmpty { ss.isEnabled = false }

        let eng = m.addItem(withTitle: "Engagements", action: nil, keyEquivalent: "")
        eng.target = self
        let sub = NSMenu()
        for pr in profs {
            let it = sub.addItem(withTitle: pr.name,
                                 action: #selector(pickProfile(_:)), keyEquivalent: "")
            it.target = self
            it.state = pr.name == selected ? .on : .off
        }
        sub.addItem(.separator())
        let newIt = sub.addItem(withTitle: "＋ New engagement…",
                                action: #selector(newEngagement), keyEquivalent: "n")
        newIt.target = self
        let edIt = sub.addItem(withTitle: "Edit profiles folder…",
                               action: #selector(editProfiles), keyEquivalent: "")
        edIt.target = self
        m.setSubmenu(sub, for: eng)

        m.addItem(.separator())
        for entry in [
            ("Copy client env", #selector(copyEnv)),
            ("Open audit dashboard", #selector(openAudit)),
            ("Verify coverage", #selector(verifyCoverage)),
            ("Export & archive vault…", #selector(exportArchive)),
        ] {
            let it = m.addItem(withTitle: entry.0, action: entry.1, keyEquivalent: "")
            it.target = self
        }
        m.addItem(.separator())
        let q = m.addItem(withTitle: "Quit Anonbar",
                          action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        q.target = nil
        refreshTitle()
    }

    // -------------------------------------------------------------- actions
    @objc func toggleStart() {
        if runningChild() {
            stopChild(notify: false)
        } else if !selected.isEmpty {
            startChild(selected)
        }
    }

    @objc func pickProfile(_ sender: NSMenuItem) {
        let name = sender.title
        guard profile(named: name) != nil else { return }
        selected = name
        if runningChild() {
            let old = childProfile ?? "?"
            stopChild(notify: false)
            startChild(name)
            alertBox("Switched engagement", "\(old) → \(name)\n(vaults stay isolated)")
        }
        menuNeedsUpdate(menu)
    }

    @objc func newEngagement() {
        let a = NSAlert()
        a.messageText = "New engagement"
        a.informativeText = "One JSON profile per client/test — the name becomes the isolated vault."

        let name = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        name.placeholderString = "e.g. acme-web"
        let scope = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        scope.placeholderString = "acme.com, DC01, Acme Corp (optional)"
        let port = NSTextField(frame: NSRect(x: 0, y: 0, width: 80, height: 24))
        port.stringValue = "8099"
        let notes = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        notes.placeholderString = "what is this test?"
        let eph = NSButton(checkboxWithTitle: "Ephemeral vault (nothing on disk)",
                           target: nil, action: nil)
        eph.state = .off

        let grid = NSGridView(numberOfColumns: 2, rows: 0)
        grid.addRow(with: [NSTextField(labelWithString: "Name"), name])
        grid.addRow(with: [NSTextField(labelWithString: "Scope terms"), scope])
        grid.addRow(with: [NSTextField(labelWithString: "Port"), port])
        grid.addRow(with: [NSTextField(labelWithString: "Notes"), notes])
        grid.addRow(with: [NSTextField(labelWithString: ""), eph])
        grid.column(at: 1).width = 260
        grid.frame = NSRect(x: 0, y: 0, width: 360, height: CGFloat(grid.numberOfRows) * 30 + 10)
        a.accessoryView = grid
        a.addButton(withTitle: "Create & start")
        a.addButton(withTitle: "Cancel")

        if a.runModal() != .alertFirstButtonReturn { return }
        let nm = name.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !nm.isEmpty else { return }
        let portVal = Int(port.stringValue.trimmingCharacters(in: .whitespaces)) ?? 8099

        var argv = ["profile", "new", nm, "--port", String(portVal)]
        let sc = scope.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if !sc.isEmpty { argv += ["--scope", sc] }
        if !notes.stringValue.isEmpty { argv += ["--notes", notes.stringValue] }
        if eph.state == .on { argv += ["--ephemeral"] }

        let (rc, out) = cliSync(argv)
        if rc != 0 {
            alertBox("Could not create profile", out)
            return
        }
        selected = nm
        startChild(nm)
        menuNeedsUpdate(menu)
    }

    @objc func editProfiles() {
        NSWorkspace.shared.open(profilesDir)
    }

    @objc func copyEnv() {
        cliAsync(["env", selected]) { _, out in
            let lines = out.split(separator: "\n")
                .map(String.init)
                .filter { $0.hasPrefix("export ") }
            let text = lines.joined(separator: "\n")
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        }
    }

    @objc func openAudit() {
        let port = profile(named: selected)?.port ?? 8099
        var url = "http://127.0.0.1:\(port)/audit"
        if let tok = ProcessInfo.processInfo.environment["ANONPROXY_API_TOKEN"],
           !tok.isEmpty {
            url += "#token=" + tok.addingPercentEncoding(
                withAllowedCharacters: .alphanumerics)!
        }
        NSWorkspace.shared.open(URL(string: url)!)
    }

    @objc func verifyCoverage() {
        cliAsync(["verify"]) { rc, out in
            let lines = out.split(separator: "\n").suffix(2)
                .map(String.init).joined(separator: " · ")
            alertBox(rc == 0 ? "Verify PASS ✓" : "Verify FAIL ✗",
                     String(lines.prefix(280)))
        }
    }

    @objc func exportArchive() {
        guard !selected.isEmpty else { return }
        cliAsync(["close", selected]) { rc, out in
            alertBox(rc == 0 ? "Audited & closed: \(self.selected)"
                             : "Close-out failed", String(out.suffix(400)))
        }
    }

    // debug: --debug prints resolved config and exits without UI
    static func debugRun(_ d: AppDelegate) {
        print("home=\(d.home.path)")
        print("source=\(d.homeSource)")
        print("python=\(d.pythonBin)")
        print("profiles=\(d.profiles().map { $0.name }.joined(separator: ","))")
        print("selected=\(d.selected)")
    }
}

// MARK: - main

let app = NSApplication.shared
let delegate = AppDelegate()

if CommandLine.arguments.contains("--ensure-venv") {
    delegate.ensureVenv { ok, out in
        print("venv:", delegate.venvPy)
        print(ok ? "READY" : "FAILED:\n\(out.suffix(600))")
        exit(ok ? 0 : 1)
    }
    dispatchMain()
}

if CommandLine.arguments.contains("--debug") {
    delegate.selected = (try? String(
        contentsOf: delegate.selectionPath(), encoding: .utf8)) ?? ""
    AppDelegate.debugRun(delegate)
    exit(0)
}

app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()

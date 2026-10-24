// Reelcast macOS helper.
//
//   reelcast-helper tap [--listen]   Records only Spotify's audio via a Core Audio process tap
//                                    (macOS 14.2+) and writes float32 stereo PCM at 48 kHz to
//                                    stdout – the same format pw-record delivers on Linux.
//                                    Without --listen, Spotify is muted while tapped.
//                                    Events go to stderr as JSON lines.
//   reelcast-helper state [--interval S]
//                                    Streams Spotify's player state as JSON lines
//                                    ("pending": true while macOS asks for permission).
//   reelcast-helper control <command> [argument...]
//                                    play | pause | next | open URI [CONTEXT] | seek SECONDS |
//                                    volume 0-100 | quit
//   reelcast-helper check            Permission and device overview as JSON.

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

let outRate: Double = 48000
let outChannels: AVAudioChannelCount = 2

func emit(_ dict: [String: Any], to handle: FileHandle = .standardError) {
    guard let data = try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys]) else { return }
    handle.write(data + Data([0x0A]))
}

func fail(_ message: String) -> Never {
    emit(["event": "error", "message": message])
    exit(1)
}

// MARK: - Core Audio helpers

func address(_ selector: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func objectList(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> [AudioObjectID] {
    var addr = address(selector)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(object, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func stringProperty(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = address(selector)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var value: Unmanaged<CFString>?
    let status = withUnsafeMutablePointer(to: &value) {
        AudioObjectGetPropertyData(object, &addr, 0, nil, &size, $0)
    }
    guard status == noErr, let v = value else { return nil }
    return v.takeRetainedValue() as String
}

func scalarProperty<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ initial: T) -> T? {
    var addr = address(selector)
    var size = UInt32(MemoryLayout<T>.size)
    var value = initial
    guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr else { return nil }
    return value
}

let systemObject = AudioObjectID(kAudioObjectSystemObject)

/// Audio process objects that belong to Spotify (main app and helpers).
func spotifyProcesses() -> [AudioObjectID] {
    objectList(systemObject, kAudioHardwarePropertyProcessObjectList).filter {
        (stringProperty($0, kAudioProcessPropertyBundleID) ?? "").hasPrefix("com.spotify.client")
    }.sorted()
}

func defaultOutputUID() -> String? {
    guard let dev: AudioObjectID = scalarProperty(systemObject, kAudioHardwarePropertyDefaultSystemOutputDevice,
                                                  AudioObjectID(0)), dev != 0 else { return nil }
    return stringProperty(dev, kAudioDevicePropertyDeviceUID)
}

// MARK: - PCM output (writer thread, so the audio thread never blocks)

final class PCMWriter {
    private var pending = Data()
    private let lock = NSLock()
    private let ready = DispatchSemaphore(value: 0)

    init() {
        let thread = Thread { [unowned self] in self.run() }
        thread.name = "pcm-writer"
        thread.start()
    }

    func append(_ data: Data) {
        lock.lock()
        pending.append(data)
        lock.unlock()
        ready.signal()
    }

    private func run() {
        while true {
            ready.wait()
            lock.lock()
            let chunk = pending
            pending.removeAll(keepingCapacity: true)
            lock.unlock()
            if chunk.isEmpty { continue }
            let ok = chunk.withUnsafeBytes { raw -> Bool in
                var offset = 0
                while offset < raw.count {
                    let n = write(STDOUT_FILENO, raw.baseAddress! + offset, raw.count - offset)
                    if n <= 0 { return false }
                    offset += n
                }
                return true
            }
            if !ok { exit(0) }  // reader went away
        }
    }
}

// MARK: - Process tap

final class SpotifyTap {
    let listen: Bool
    let writer = PCMWriter()
    private let control = DispatchQueue(label: "reelcast.tap.control")
    // Creating a tap blocks for as long as macOS asks whether Reelcast may record audio – so
    // it runs on its own queue, and the silence padding keeps the stream going meanwhile.
    private let builder = DispatchQueue(label: "reelcast.tap.build")
    private var building = false  // control queue only
    private var processes: [AudioObjectID] = []
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var ioProc: AudioDeviceIOProcID?
    private var converter: AVAudioConverter?
    private var inFormat: AVAudioFormat?
    private var passthrough = false
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: outRate,
                                          channels: outChannels, interleaved: true)!
    private var timer: DispatchSourceTimer?
    private var ticks = 0

    // Shared between the real-time IO thread and the control queue.
    private let lock = NSLock()
    private var lastIO: UInt64 = 0  // uptime (ns) of the last IO callback
    private var paddedUntil: UInt64 = 0  // uptime (ns) up to which silence was written
    private var paddedSinceIO = 0  // frames of silence written since the last IO callback
    private var nextSampleTime = -1.0  // IO thread only: where the next buffer should start
    private var callbacks = 0
    private var framesFilled = 0  // silence for buffers Core Audio dropped (overload)
    private var framesIn = 0
    private var framesOut = 0
    private var lastIssue = ""
    private var lastStats: UInt64 = 0

    init(listen: Bool) { self.listen = listen }

    private func now() -> UInt64 { DispatchTime.now().uptimeNanoseconds }

    func start() {
        paddedUntil = now()
        let t = DispatchSource.makeTimerSource(queue: control)
        t.schedule(deadline: .now(), repeating: .milliseconds(10))
        t.setEventHandler { [weak self] in self?.tick() }
        t.resume()
        timer = t
        emit(["event": "started", "rate": outRate, "listen": listen])
    }

    /// Every 10 ms: look for Spotify's processes (every 0.5 s) and keep the stream
    /// continuous – while no audio arrives (no tap yet, Spotify paused), write silence
    /// in real time, just like PipeWire's monitor does on Linux.
    private func tick() {
        ticks += 1
        if ticks % 50 == 1 && !building {
            let current = spotifyProcesses()
            if current != processes {
                processes = current
                building = true
                builder.async { [weak self] in
                    self?.rebuild(current)
                    self?.control.async { self?.building = false }
                }
            }
        }
        let t = now()
        var pad = 0
        var stats: [String: Any]?
        lock.lock()
        if t &- lastIO > 250_000_000 || lastIO == 0 {
            let from = max(paddedUntil, lastIO)
            if t > from {
                pad = Int(Double(t - from) / 1e9 * outRate)
                if pad > 0 {
                    paddedUntil = from + UInt64(Double(pad) / outRate * 1e9)
                    paddedSinceIO += pad
                }
            }
        }
        if tapID != kAudioObjectUnknown, t &- lastStats > 3_000_000_000 {
            lastStats = t
            stats = ["event": "stats", "callbacks": callbacks, "framesIn": framesIn,
                     "framesOut": framesOut, "framesFilled": framesFilled, "issue": lastIssue]
        }
        lock.unlock()
        if pad > 0 { writer.append(Data(count: pad * Int(outChannels) * 4)) }
        if let s = stats { emit(s) }
    }

    private func teardown() {
        if aggregateID != kAudioObjectUnknown {
            if let proc = ioProc {
                AudioDeviceStop(aggregateID, proc)
                AudioDeviceDestroyIOProcID(aggregateID, proc)
            }
            AudioHardwareDestroyAggregateDevice(aggregateID)
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
        }
        ioProc = nil
        aggregateID = AudioObjectID(kAudioObjectUnknown)
        tapID = AudioObjectID(kAudioObjectUnknown)
        converter = nil
        nextSampleTime = -1
    }

    private func rebuild(_ current: [AudioObjectID]) {
        teardown()
        guard !current.isEmpty else {
            emit(["event": "tap", "processes": 0])
            return
        }
        let desc = CATapDescription(stereoMixdownOfProcesses: current)
        desc.uuid = UUID()
        desc.name = "Reelcast"
        desc.isPrivate = true
        desc.muteBehavior = listen ? .unmuted : .mutedWhenTapped
        var tap = AudioObjectID(kAudioObjectUnknown)
        var status = AudioHardwareCreateProcessTap(desc, &tap)
        guard status == noErr else {
            emit(["event": "error", "message": "AudioHardwareCreateProcessTap failed (\(status)) – "
                  + "is \"System Audio Recording\" allowed for Reelcast?"])
            return
        }
        tapID = tap
        // Aggregate device that contains only the tap: no microphone or other inputs end up
        // in it (and macOS doesn't ask for microphone access).
        var aggregate: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Reelcast Tap",
            kAudioAggregateDeviceUIDKey: "reelcast.tap." + UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                              kAudioSubTapUIDKey: desc.uuid.uuidString]],
        ]
        if ProcessInfo.processInfo.environment["REELCAST_TAP_WITH_OUTPUT"] != nil, let outputUID = defaultOutputUID() {
            aggregate[kAudioAggregateDeviceMainSubDeviceKey] = outputUID
            aggregate[kAudioAggregateDeviceSubDeviceListKey] = [[kAudioSubDeviceUIDKey: outputUID]]
        }
        var agg = AudioObjectID(kAudioObjectUnknown)
        status = AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &agg)
        guard status == noErr else {
            emit(["event": "error", "message": "AudioHardwareCreateAggregateDevice failed (\(status))"])
            return
        }
        aggregateID = agg

        guard var asbd: AudioStreamBasicDescription = scalarProperty(tap, kAudioTapPropertyFormat,
                                                                      AudioStreamBasicDescription()),
              let format = AVAudioFormat(streamDescription: &asbd),
              let conv = AVAudioConverter(from: format, to: outFormat) else {
            emit(["event": "error", "message": "unsupported tap format"])
            return
        }
        inFormat = format
        converter = conv
        passthrough = format.commonFormat == .pcmFormatFloat32 && format.sampleRate == outRate
            && format.channelCount == outChannels && format.isInterleaved
        // The IO block runs on Core Audio's real-time thread (no queue): it only copies data.
        status = AudioDeviceCreateIOProcIDWithBlock(&ioProc, agg, nil) { [weak self] _, input, inputTime, _, _ in
            self?.process(input, at: inputTime.pointee)
        }
        guard status == noErr, let proc = ioProc else {
            emit(["event": "error", "message": "AudioDeviceCreateIOProcIDWithBlock failed (\(status))"])
            return
        }
        status = AudioDeviceStart(agg, proc)
        emit(["event": "tap", "processes": current.count, "inputRate": format.sampleRate,
              "channels": Int(format.channelCount), "interleaved": format.isInterleaved,
              "passthrough": passthrough, "status": Int(status)])
    }

    private func process(_ input: UnsafePointer<AudioBufferList>, at time: AudioTimeStamp) {
        let t = now()
        lock.lock()
        lastIO = t
        if paddedUntil < t { paddedUntil = t }
        let padded = paddedSinceIO
        paddedSinceIO = 0
        callbacks += 1
        lock.unlock()
        guard let format = inFormat else { return }
        fillDropout(at: time, input: input, format: format, padded: padded)
        var list = UnsafeMutablePointer(mutating: input)
        let all = UnsafeMutableAudioBufferListPointer(list)
        let expected = format.isInterleaved ? 1 : Int(format.channelCount)
        var subset: UnsafeMutableAudioBufferListPointer?
        if all.count > expected {
            // Inputs of other devices come first; the tap's buffers are the last ones.
            let sub = AudioBufferList.allocate(maximumBuffers: expected)
            for i in 0..<expected { sub[i] = all[all.count - expected + i] }
            subset = sub
            list = sub.unsafeMutablePointer
        }
        defer { if let sub = subset { free(sub.unsafeMutablePointer) } }
        let buffers = UnsafeMutableAudioBufferListPointer(list)

        if passthrough, let first = buffers.first, let data = first.mData {
            let bytes = Int(first.mDataByteSize)
            writer.append(Data(bytes: data, count: bytes))
            let frames = bytes / (Int(outChannels) * 4)
            lock.lock()
            framesIn += frames
            framesOut += frames
            lock.unlock()
            return
        }
        guard let conv = converter,
              let inBuf = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: list, deallocator: nil) else {
            lock.lock(); lastIssue = "buffer wrap failed (buffers: \(all.count))"; lock.unlock()
            return
        }
        let bytesPerFrame = format.streamDescription.pointee.mBytesPerFrame
        let byteSize = buffers.first?.mDataByteSize ?? 0
        inBuf.frameLength = bytesPerFrame > 0 ? byteSize / bytesPerFrame : 0
        let capacity = AVAudioFrameCount(Double(inBuf.frameLength) * outRate / format.sampleRate) + 16
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }
        var consumed = false
        var error: NSError?
        let result = conv.convert(to: outBuf, error: &error) { _, status in
            if consumed {
                status.pointee = .noDataNow
                return nil
            }
            consumed = true
            status.pointee = .haveData
            return inBuf
        }
        let n = Int(outBuf.frameLength) * Int(outChannels)
        if n > 0, let ptr = outBuf.floatChannelData?[0] {
            writer.append(Data(bytes: ptr, count: n * 4))
        }
        lock.lock()
        if result == .error { lastIssue = "convert: \(error?.localizedDescription ?? "?")" }
        framesIn += Int(inBuf.frameLength)
        framesOut += Int(outBuf.frameLength)
        lock.unlock()
    }

    /// When the system is overloaded, Core Audio drops whole IO cycles; the next buffer's
    /// sample time then jumps. Write silence for the missing part so the recording keeps
    /// time with Spotify's position (minus what the idle padding already wrote).
    private func fillDropout(at time: AudioTimeStamp, input: UnsafePointer<AudioBufferList>,
                             format: AVAudioFormat, padded: Int) {
        guard time.mFlags.contains(.sampleTimeValid) else { return }
        let bytesPerFrame = Int(format.streamDescription.pointee.mBytesPerFrame)
        let first = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input)).last
        let frames = bytesPerFrame > 0 ? Int(first?.mDataByteSize ?? 0) / bytesPerFrame : 0
        defer { nextSampleTime = time.mSampleTime + Double(frames) }
        guard nextSampleTime >= 0 else { return }
        let gap = time.mSampleTime - nextSampleTime
        guard gap >= 1, gap < format.sampleRate * 10 else { return }
        let missing = Int(gap * outRate / format.sampleRate) - padded
        guard missing > 0 else { return }
        writer.append(Data(count: missing * Int(outChannels) * 4))
        lock.lock()
        framesFilled += missing
        lock.unlock()
    }

    func stop() {
        control.sync { timer?.cancel() }
        let done = DispatchSemaphore(value: 0)
        builder.async { [weak self] in
            self?.teardown()
            done.signal()
        }
        _ = done.wait(timeout: .now() + 2)  // a tap still waiting for permission dies with us
    }
}

// MARK: - Spotify via AppleScript

let separator = "\u{1F}"

func runScript(_ source: String) -> (String?, String?) {
    var error: NSDictionary?
    guard let script = NSAppleScript(source: source) else { return (nil, "could not compile script") }
    let result = script.executeAndReturnError(&error)
    if let e = error {
        return (nil, (e[NSAppleScript.errorMessage] as? String) ?? "\(e)")
    }
    return (result.stringValue, nil)
}

// Spotify answers every property in an Apple Event of its own, and in a busy system a full
// query takes seconds. So the position is fetched on its own and timed (the sample time is
// the middle of that one event), and the track's details only when the track changes.
let statusScript = """
if application "Spotify" is running then
    tell application "Spotify"
        set reply to (player state as string) & "\(separator)" & sound volume
        try
            set reply to reply & "\(separator)" & (id of current track)
        end try
        return reply
    end tell
else
    return "not running"
end if
"""

let trackScript = """
if application "Spotify" is running then
    tell application "Spotify"
        set trk to current track
        return (id of trk) & "\(separator)" & (name of trk) & "\(separator)" & (artist of trk) & "\(separator)" & (album of trk) & "\(separator)" & (album artist of trk) & "\(separator)" & (track number of trk) & "\(separator)" & (disc number of trk) & "\(separator)" & (duration of trk) & "\(separator)" & (artwork url of trk)
    end tell
else
    return "not running"
end if
"""

let positionScript = """
if application "Spotify" is running then
    tell application "Spotify" to set p to player position
    return (round (p * 1000)) as text
else
    return "not running"
end if
"""

/// Whether Spotify's main executable is running – from the process list, so no Apple Events needed.
func spotifyRunning() -> Bool {
    var pids = [pid_t](repeating: 0, count: Int(max(proc_listallpids(nil, 0), 0)) + 64)
    let n = Int(proc_listallpids(&pids, Int32(pids.count * MemoryLayout<pid_t>.size)))
    var path = [CChar](repeating: 0, count: 4096)
    for pid in pids.prefix(max(n, 0)) where pid > 0 {
        if proc_pidpath(pid, &path, UInt32(path.count)) > 0,
           String(cString: path).hasSuffix("/Spotify.app/Contents/MacOS/Spotify") { return true }
    }
    return false
}

/// The `state` stream. The first Apple Event to Spotify blocks while macOS asks the user to
/// allow controlling Spotify (or while the screen is locked and it can't ask), so until
/// Spotify has answered once, a watchdog keeps the stream alive: "running" from the process
/// list, plus "pending": true.
final class StateStream: @unchecked Sendable {
    private let lock = NSLock()
    private var lastSent = Date()
    private var answered = false

    init() {
        let thread = Thread { [unowned self] in self.watch() }
        thread.name = "state-watchdog"
        thread.start()
    }

    func send(_ state: [String: Any], answered: Bool = false) {
        var state = state
        if state["t"] == nil { state["t"] = Date().timeIntervalSince1970 }
        lock.lock()
        emit(state, to: .standardOutput)
        lastSent = Date()
        self.answered = self.answered || answered
        lock.unlock()
    }

    private func watch() {
        while true {
            Thread.sleep(forTimeInterval: 0.5)
            lock.lock()
            let waiting = !answered && Date().timeIntervalSince(lastSent) > 1.5
            lock.unlock()
            if waiting {
                send(spotifyRunning() ? ["running": true, "pending": true] : ["running": false])
            }
        }
    }
}

final class SpotifyState {
    private let status: NSAppleScript
    private let track: NSAppleScript
    private let position: NSAppleScript
    private var trackID = ""
    private var trackInfo: [String: Any] = [:]

    init?() {
        guard let status = NSAppleScript(source: statusScript), let track = NSAppleScript(source: trackScript),
              let position = NSAppleScript(source: positionScript) else { return nil }
        for script in [status, track, position] {
            var error: NSDictionary?
            script.compileAndReturnError(&error)
        }
        (self.status, self.track, self.position) = (status, track, position)
    }

    private func run(_ script: NSAppleScript) -> (String?, NSDictionary?) {
        var error: NSDictionary?
        let result = script.executeAndReturnError(&error)
        return (error == nil ? result.stringValue : nil, error)
    }

    /// One sample, or nil when Spotify didn't answer every part (the caller just tries again).
    func sample() -> [String: Any]? {
        let (text, error) = run(status)
        if let e = error {
            return ["running": true, "error": (e[NSAppleScript.errorMessage] as? String) ?? "AppleScript error",
                    "errorNumber": (e[NSAppleScript.errorNumber] as? Int) ?? 0]
        }
        guard let text = text, text != "not running" else { return ["running": false] }
        let f = text.components(separatedBy: separator)
        let id = f.count > 2 ? f[2] : ""
        if id != trackID {
            trackID = ""
            trackInfo = [:]
            if !id.isEmpty {
                guard let details = run(track).0?.components(separatedBy: separator),
                      details.count >= 9, details[0] == id else { return nil }
                trackInfo = [
                    "id": id, "name": details[1], "artist": details[2], "album": details[3],
                    "albumArtist": details[4], "trackNumber": Int(details[5]) ?? 0,
                    "discNumber": Int(details[6]) ?? 0, "durationMs": Int(details[7]) ?? 0,
                    "artworkUrl": details[8],
                ]
            }
            trackID = id
        }
        let before = Date().timeIntervalSince1970
        let (ms, positionError) = run(position)
        let after = Date().timeIntervalSince1970
        guard positionError == nil, let ms = ms, let value = Double(ms) else { return nil }
        var state: [String: Any] = [
            "running": true, "status": f[0], "volume": Int(f.count > 1 ? f[1] : "") ?? 100,
            "position": value / 1000.0, "t": (before + after) / 2, "posSpan": after - before,
        ]
        state.merge(trackInfo) { current, _ in current }
        return state
    }
}

func quoted(_ s: String) -> String {
    "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
}

func control(_ args: [String]) -> Int32 {
    guard let command = args.first else { fail("control: missing command") }
    let arg = args.count > 1 ? args[1] : ""
    let body: String
    switch command {
    case "play": body = "play"
    case "pause": body = "pause"
    case "next": body = "next track"
    case "open":
        body = "play track \(quoted(arg))" + (args.count > 2 ? " in context \(quoted(args[2]))" : "")
    case "seek": body = "set player position to \(Double(arg) ?? 0)"
    case "volume": body = "set sound volume to \(Int(arg) ?? 100)"
    case "quit": body = "quit"
    default: fail("control: unknown command \(command)")
    }
    let (_, err) = runScript("tell application \"Spotify\" to \(body)")
    if let err = err {
        emit(["event": "error", "message": err])
        return 1
    }
    return 0
}

// MARK: - main

signal(SIGPIPE, SIG_IGN)
let args = Array(CommandLine.arguments.dropFirst())
guard let mode = args.first else {
    fail("usage: reelcast-helper tap [--listen] | state [--interval S] | control CMD [ARG] | check")
}

switch mode {
case "tap":
    let tap = SpotifyTap(listen: args.contains("--listen"))
    var signalSources: [DispatchSourceSignal] = []
    for sig in [SIGTERM, SIGINT] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler {
            tap.stop()
            emit(["event": "stopped"])
            exit(0)
        }
        source.resume()
        signalSources.append(source)
    }
    tap.start()
    dispatchMain()

case "state":
    var interval = 0.1
    if let i = args.firstIndex(of: "--interval"), i + 1 < args.count, let v = Double(args[i + 1]) { interval = v }
    guard let spotify = SpotifyState() else { fail("could not compile the state scripts") }
    let stream = StateStream()
    while true {  // NSAppleScript stays on the main thread
        if let state = spotify.sample() {
            stream.send(state, answered: state["running"] as? Bool == true)
        }
        Thread.sleep(forTimeInterval: interval)
    }

case "control":
    exit(control(Array(args.dropFirst())))

case "check":
    let procs = spotifyProcesses().map { ["id": Int($0), "bundle": stringProperty($0, kAudioProcessPropertyBundleID) ?? ""] }
    emit(["macOS": ProcessInfo.processInfo.operatingSystemVersionString,
          "spotifyAudioProcesses": procs,
          "defaultOutput": defaultOutputUID() ?? ""], to: .standardOutput)

default:
    fail("unknown mode \(mode)")
}

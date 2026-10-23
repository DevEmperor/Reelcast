// Reelcast macOS helper.
//
//   reelcast-helper tap [--listen]   Records only Spotify's audio via a Core Audio process tap
//                                    (macOS 14.2+) and writes float32 stereo PCM at 48 kHz to
//                                    stdout – the same format pw-record delivers on Linux.
//                                    Without --listen, Spotify is muted while tapped.
//                                    Events go to stderr as JSON lines.
//   reelcast-helper state [--interval S]
//                                    Streams Spotify's player state as JSON lines.
//   reelcast-helper control <command> [argument]
//                                    play | pause | open URI | seek SECONDS | volume 0-100 | quit
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
    private let queue = DispatchQueue(label: "reelcast.tap")
    private var processes: [AudioObjectID] = []
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var ioProc: AudioDeviceIOProcID?
    private var converter: AVAudioConverter?
    private var inFormat: AVAudioFormat?
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: outRate,
                                          channels: outChannels, interleaved: true)!
    private var lastAudio = DispatchTime.now()
    private var silenceTimer: DispatchSourceTimer?
    private var silenceClock = DispatchTime.now()

    init(listen: Bool) { self.listen = listen }

    func start() {
        // Keep the stream going with silence while Spotify has no audio process (yet).
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: .milliseconds(10))
        timer.setEventHandler { [weak self] in self?.tick() }
        timer.resume()
        silenceTimer = timer
        emit(["event": "started", "rate": outRate, "listen": listen])
    }

    private func tick() {
        let current = spotifyProcesses()
        if current != processes {
            rebuild(current)
        }
        if tapID == kAudioObjectUnknown {
            // write silence in real time
            let now = DispatchTime.now()
            let elapsed = Double(now.uptimeNanoseconds - silenceClock.uptimeNanoseconds) / 1e9
            let frames = Int(elapsed * outRate)
            if frames > 0 {
                writer.append(Data(count: frames * Int(outChannels) * 4))
                silenceClock = silenceClock + .nanoseconds(Int(Double(frames) / outRate * 1e9))
            }
        }
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
    }

    private func rebuild(_ current: [AudioObjectID]) {
        teardown()
        processes = current
        silenceClock = DispatchTime.now()
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
        guard let outputUID = defaultOutputUID() else {
            emit(["event": "error", "message": "no default output device"])
            return
        }
        let aggregate: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Reelcast Tap",
            kAudioAggregateDeviceUIDKey: "reelcast.tap." + UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                              kAudioSubTapUIDKey: desc.uuid.uuidString]],
        ]
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
        status = AudioDeviceCreateIOProcIDWithBlock(&ioProc, agg, queue) { [weak self] _, input, _, _, _ in
            self?.process(input)
        }
        guard status == noErr, let proc = ioProc else {
            emit(["event": "error", "message": "AudioDeviceCreateIOProcIDWithBlock failed (\(status))"])
            return
        }
        status = AudioDeviceStart(agg, proc)
        emit(["event": "tap", "processes": current.count, "inputRate": format.sampleRate,
              "status": Int(status)])
    }

    private func process(_ input: UnsafePointer<AudioBufferList>) {
        guard let conv = converter, let format = inFormat else { return }
        let list = UnsafeMutablePointer(mutating: input)
        guard let inBuf = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: list, deallocator: nil) else { return }
        let bytesPerFrame = format.streamDescription.pointee.mBytesPerFrame
        let byteSize = UnsafeMutableAudioBufferListPointer(list).first?.mDataByteSize ?? 0
        inBuf.frameLength = bytesPerFrame > 0 ? byteSize / bytesPerFrame : 0
        let capacity = AVAudioFrameCount(Double(inBuf.frameLength) * outRate / format.sampleRate) + 16
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }
        var consumed = false
        var error: NSError?
        conv.convert(to: outBuf, error: &error) { _, status in
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
    }

    func stop() {
        queue.sync { teardown() }
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

let stateScript = """
if application "Spotify" is running then
    tell application "Spotify"
        set playerStateText to player state as string
        set positionMs to round ((player position) * 1000)
        set volumeLevel to sound volume
        set reply to playerStateText & "\(separator)" & positionMs & "\(separator)" & volumeLevel
        try
            set trk to current track
            set reply to reply & "\(separator)" & (id of trk) & "\(separator)" & (name of trk) & "\(separator)" & (artist of trk) & "\(separator)" & (album of trk) & "\(separator)" & (album artist of trk) & "\(separator)" & (track number of trk) & "\(separator)" & (disc number of trk) & "\(separator)" & (duration of trk) & "\(separator)" & (artwork url of trk)
        end try
        return reply
    end tell
else
    return "not running"
end if
"""

func spotifyState(script: NSAppleScript) -> [String: Any] {
    var error: NSDictionary?
    let result = script.executeAndReturnError(&error)
    if let e = error {
        return ["running": true, "error": (e[NSAppleScript.errorMessage] as? String) ?? "AppleScript error",
                "errorNumber": (e[NSAppleScript.errorNumber] as? Int) ?? 0]
    }
    guard let text = result.stringValue, text != "not running" else { return ["running": false] }
    let f = text.components(separatedBy: separator)
    var state: [String: Any] = [
        "running": true,
        "status": f[0],
        "position": (Double(f[1]) ?? 0) / 1000.0,
        "volume": Int(f[2]) ?? 100,
    ]
    if f.count >= 12 {
        state["id"] = f[3]
        state["name"] = f[4]
        state["artist"] = f[5]
        state["album"] = f[6]
        state["albumArtist"] = f[7]
        state["trackNumber"] = Int(f[8]) ?? 0
        state["discNumber"] = Int(f[9]) ?? 0
        state["durationMs"] = Int(f[10]) ?? 0
        state["artworkUrl"] = f[11]
    }
    return state
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
    case "open": body = "play track \(quoted(arg))"
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
    guard let script = NSAppleScript(source: stateScript) else { fail("could not compile state script") }
    var compileError: NSDictionary?
    script.compileAndReturnError(&compileError)
    while true {
        var state = spotifyState(script: script)
        state["t"] = Date().timeIntervalSince1970
        emit(state, to: .standardOutput)
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

// "Reelcast Dev.app" – development launcher for testing in a macOS session.
//
// macOS grants audio recording and Apple Events only to app bundles that declare
// why (NSAudioCaptureUsageDescription / NSAppleEventsUsageDescription). This tiny,
// stable launcher is that bundle: it runs the development Python with the given
// arguments as a child process, so permissions stay granted while the Python
// code changes.  Usage:  open -W "Reelcast Dev.app" --args -m reelcast --gui

import Foundation

let home = NSHomeDirectory()
let task = Process()
task.executableURL = URL(fileURLWithPath: home + "/reelcast-dev/venv/bin/python")
task.arguments = Array(CommandLine.arguments.dropFirst())
task.currentDirectoryURL = URL(fileURLWithPath: home + "/reelcast-dev/src")
var env = ProcessInfo.processInfo.environment
env["PATH"] = home + "/reelcast-dev/ffbuild:" + home + "/reelcast-dev/bin:/usr/bin:/bin:/usr/sbin:/sbin"
env["PYTHONUNBUFFERED"] = "1"
task.environment = env
do {
    try task.run()
} catch {
    FileHandle.standardError.write("could not start python: \(error)\n".data(using: .utf8)!)
    exit(1)
}
task.waitUntilExit()
exit(task.terminationStatus)

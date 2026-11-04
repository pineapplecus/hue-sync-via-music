// setup_audio: route the Mac's system audio into BlackHole while still playing it on your speakers.
//
//   setup_audio          create the "Hue Sync Output" Multi-Output Device (current speakers + BlackHole 2ch)
//                        and make it the default output. Safe to run again: it reuses the existing one.
//   setup_audio remove   switch the default output back to the built-in speakers and delete the device.

import CoreAudio
import Foundation

let ourUID = "hue-sync-output"
let systemObject = AudioObjectID(kAudioObjectSystemObject)

func address(_ sel: AudioObjectPropertySelector,
             scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func allDevices() -> [AudioDeviceID] {
    var a = address(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(systemObject, &a, 0, nil, &size)
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(systemObject, &a, 0, nil, &size, &ids)
    return ids
}

func string(_ d: AudioDeviceID, _ sel: AudioObjectPropertySelector) -> String {
    var a = address(sel)
    var s: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    AudioObjectGetPropertyData(d, &a, 0, nil, &size, &s)
    return (s?.takeRetainedValue() as String?) ?? ""
}

func hasOutput(_ d: AudioDeviceID) -> Bool {
    var a = address(kAudioDevicePropertyStreams, scope: kAudioObjectPropertyScopeOutput)
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(d, &a, 0, nil, &size)
    return size > 0
}

func isBuiltIn(_ d: AudioDeviceID) -> Bool {
    var a = address(kAudioDevicePropertyTransportType)
    var t: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    AudioObjectGetPropertyData(d, &a, 0, nil, &size, &t)
    return t == kAudioDeviceTransportTypeBuiltIn
}

func defaultOutput() -> AudioDeviceID {
    var a = address(kAudioHardwarePropertyDefaultOutputDevice)
    var d = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(systemObject, &a, 0, nil, &size, &d)
    return d
}

@discardableResult
func setDefaultOutput(_ d: AudioDeviceID) -> OSStatus {
    var a = address(kAudioHardwarePropertyDefaultOutputDevice)
    var dev = d
    return AudioObjectSetPropertyData(systemObject, &a, 0, nil, UInt32(MemoryLayout<AudioDeviceID>.size), &dev)
}

let devices = allDevices()
let ours = devices.first { string($0, kAudioDevicePropertyDeviceUID) == ourUID }
let builtIn = devices.first { isBuiltIn($0) && hasOutput($0) }
let mode = CommandLine.arguments.dropFirst().first ?? "create"

if mode == "remove" {
    if let b = builtIn { setDefaultOutput(b) }
    if let o = ours {
        let st = AudioHardwareDestroyAggregateDevice(o)
        print(st == 0 ? "Removed 'Hue Sync Output' and restored the built-in output." : "Could not remove the device: \(st)")
    } else {
        print("Nothing to remove. Output restored to the built-in speakers.")
    }
    exit(0)
}

if let o = ours {
    setDefaultOutput(o)
    print("'Hue Sync Output' already exists. Set as the default output.")
    exit(0)
}

guard let bh = devices.first(where: { string($0, kAudioObjectPropertyName) == "BlackHole 2ch" }) else {
    print("BlackHole 2ch not found. Install it (brew install blackhole-2ch) and restart the audio service."); exit(1)
}
// Play through whatever you are listening on now (speakers, headphones, AirPods...); fall back to built-in.
let current = defaultOutput()
guard let real = (current != bh && hasOutput(current)) ? current : builtIn else {
    print("Could not find a speaker or headphone output to pair with BlackHole."); exit(1)
}
let realUID = string(real, kAudioDevicePropertyDeviceUID), bhUID = string(bh, kAudioDevicePropertyDeviceUID)

let desc: [String: Any] = [
    kAudioAggregateDeviceNameKey: "Hue Sync Output",
    kAudioAggregateDeviceUIDKey: ourUID,
    kAudioAggregateDeviceIsStackedKey: 1,  // stacked = Multi-Output Device
    kAudioAggregateDeviceMasterSubDeviceKey: realUID,
    kAudioAggregateDeviceSubDeviceListKey: [
        [kAudioSubDeviceUIDKey: realUID],
        [kAudioSubDeviceUIDKey: bhUID, kAudioSubDeviceDriftCompensationKey: 1],
    ],
]
var newID: AudioObjectID = 0
let st = AudioHardwareCreateAggregateDevice(desc as CFDictionary, &newID)
guard st == 0 else { print("Could not create the output device: \(st)"); exit(1) }
let st2 = setDefaultOutput(newID)
print(st2 == 0 ? "Created 'Hue Sync Output' (\(string(real, kAudioObjectPropertyName)) + BlackHole 2ch) and set it as the default output."
               : "Created the device but could not set it as default: \(st2)")

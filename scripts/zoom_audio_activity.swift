#!/usr/bin/env swift
//
// zoom_audio_activity.swift
//
// CoreAudio process list를 조회해 Zoom 계열 프로세스가 실제 오디오 I/O를
// 실행 중인지 확인한다. 출력은 Python 쪽에서 파싱하기 쉽게 한 줄 JSON이다.

import CoreAudio
import Darwin
import Foundation

struct AudioProcessStatus {
    let pid: pid_t
    let bundleID: String
    let path: String
    let input: Bool
    let output: Bool
}

func getProcessObjectIDs() throws -> [AudioObjectID] {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )

    var dataSize: UInt32 = 0
    var status = AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject),
        &address,
        0,
        nil,
        &dataSize
    )
    guard status == noErr else {
        throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
    }

    let count = Int(dataSize) / MemoryLayout<AudioObjectID>.stride
    guard count > 0 else {
        return []
    }

    var objectIDs = Array<AudioObjectID>(repeating: 0, count: count)
    status = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject),
        &address,
        0,
        nil,
        &dataSize,
        &objectIDs
    )
    guard status == noErr else {
        throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
    }
    return objectIDs
}

func getStringProperty(_ objectID: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize = UInt32(MemoryLayout<CFString?>.size)
    var value: CFString?
    let status = withUnsafeMutablePointer(to: &value) { pointer in
        AudioObjectGetPropertyData(objectID, &address, 0, nil, &dataSize, pointer)
    }
    guard status == noErr, let value else {
        return ""
    }
    return value as String
}

func getPIDProperty(_ objectID: AudioObjectID) -> pid_t {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioProcessPropertyPID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize = UInt32(MemoryLayout<pid_t>.size)
    var pid = pid_t(0)
    let status = AudioObjectGetPropertyData(objectID, &address, 0, nil, &dataSize, &pid)
    if status != noErr {
        return 0
    }
    return pid
}

func getBoolProperty(_ objectID: AudioObjectID, _ selector: AudioObjectPropertySelector) -> Bool {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize = UInt32(MemoryLayout<UInt32>.size)
    var value = UInt32(0)
    let status = AudioObjectGetPropertyData(objectID, &address, 0, nil, &dataSize, &value)
    return status == noErr && value != 0
}

func getProcessPath(_ pid: pid_t) -> String {
    guard pid > 0 else {
        return ""
    }
    // PROC_PIDPATHINFO_MAXSIZE is a C macro that Swift does not import reliably.
    var buffer = [CChar](repeating: 0, count: 4096)
    let result = proc_pidpath(pid, &buffer, UInt32(buffer.count))
    if result <= 0 {
        return ""
    }
    return String(cString: buffer)
}

func isZoomProcess(bundleID: String, path: String) -> Bool {
    let haystack = "\(bundleID) \(path)".lowercased()
    return haystack.contains("zoom.us")
        || haystack.contains("us.zoom")
        || haystack.contains("/zoom")
        || haystack.contains("/cpt")
}

func jsonEscape(_ value: String) -> String {
    let data = try? JSONSerialization.data(withJSONObject: [value], options: [])
    guard
        let data,
        let encoded = String(data: data, encoding: .utf8),
        encoded.count >= 2
    else {
        return "\"\""
    }
    return String(encoded.dropFirst().dropLast())
}

do {
    let statuses = try getProcessObjectIDs().compactMap { objectID -> AudioProcessStatus? in
        let bundleID = getStringProperty(objectID, kAudioProcessPropertyBundleID)
        let pid = getPIDProperty(objectID)
        let path = getProcessPath(pid)
        guard isZoomProcess(bundleID: bundleID, path: path) else {
            return nil
        }

        return AudioProcessStatus(
            pid: pid,
            bundleID: bundleID,
            path: path,
            input: getBoolProperty(objectID, kAudioProcessPropertyIsRunningInput),
            output: getBoolProperty(objectID, kAudioProcessPropertyIsRunningOutput)
        )
    }

    let active = statuses.contains { $0.input || $0.output }
    let processJSON = statuses.map { status in
        """
        {"pid":\(status.pid),"bundle_id":"\(jsonEscape(status.bundleID))","path":"\(jsonEscape(status.path))","input":\(status.input),"output":\(status.output)}
        """
    }.joined(separator: ",")
    print("{\"ok\":true,\"active\":\(active),\"processes\":[\(processJSON)]}")
} catch {
    fputs("{\"ok\":false,\"active\":false,\"error\":\"\(jsonEscape(String(describing: error)))\"}\n", stderr)
    exit(2)
}

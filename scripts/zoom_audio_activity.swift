#!/usr/bin/env swift
//
// zoom_audio_activity.swift
//
// CoreAudio process list를 조회해 Zoom 계열 프로세스가 실제 오디오 I/O를
// 실행 중인지 확인한다. 출력은 Python 쪽에서 파싱하기 쉽게 한 줄 JSON이다.

import AudioToolbox
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

func getObjectIDs(_ objectID: AudioObjectID, _ selector: AudioObjectPropertySelector) throws -> [AudioObjectID] {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )

    var dataSize: UInt32 = 0
    var status = AudioObjectGetPropertyDataSize(
        objectID,
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
        objectID,
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

func getUInt32Property(_ objectID: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize = UInt32(MemoryLayout<UInt32>.size)
    var value = UInt32(0)
    let status = AudioObjectGetPropertyData(objectID, &address, 0, nil, &dataSize, &value)
    return status == noErr ? value : nil
}

func getBoolProperty(_ objectID: AudioObjectID, _ selector: AudioObjectPropertySelector) -> Bool {
    return (getUInt32Property(objectID, selector) ?? 0) != 0
}

func getInputChannelCount(_ deviceID: AudioObjectID) -> Int? {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamConfiguration,
        mScope: kAudioObjectPropertyScopeInput,
        mElement: kAudioObjectPropertyElementMain
    )
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(deviceID, &address, 0, nil, &size) == noErr,
        size >= MemoryLayout<AudioBufferList>.size else {
        return nil
    }
    let data = UnsafeMutableRawPointer.allocate(
        byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment
    )
    defer { data.deallocate() }
    guard AudioObjectGetPropertyData(deviceID, &address, 0, nil, &size, data) == noErr else {
        return nil
    }
    let buffers = UnsafeMutableAudioBufferListPointer(data.assumingMemoryBound(to: AudioBufferList.self))
    return buffers.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func aggregateSourceUIDs(_ deviceID: AudioObjectID) -> [String]? {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioAggregateDevicePropertyFullSubDeviceList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var value: Unmanaged<CFArray>?
    var size = UInt32(MemoryLayout.size(ofValue: value))
    guard AudioObjectGetPropertyData(deviceID, &address, 0, nil, &size, &value) == noErr,
        let value else {
        return nil
    }
    return (value.takeRetainedValue() as NSArray) as? [String]
}

// FullSubDeviceList 순서가 Aggregate 입력 순서다. 현재 활성 장치가 그 구성과
// 일치할 때만 물리 마이크 1채널과 BlackHole 2ch의 위치를 확정한다.
func aggregateMicChannel(_ name: String) throws -> Int? {
    let devices = try getObjectIDs(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDevices)
    let matches = devices.filter { getStringProperty($0, kAudioObjectPropertyName) == name }
    guard matches.count == 1, let aggregate = matches.first,
        getUInt32Property(aggregate, kAudioDevicePropertyTransportType) == kAudioDeviceTransportTypeAggregate,
        let orderedUIDs = aggregateSourceUIDs(aggregate), orderedUIDs.count == 2,
        getInputChannelCount(aggregate) == 3 else {
        return nil
    }
    let active = try getObjectIDs(aggregate, kAudioAggregateDevicePropertyActiveSubDeviceList)
    guard active.count == 2 else { return nil }
    let physicalTransports: Set<UInt32> = [
        kAudioDeviceTransportTypeBuiltIn, kAudioDeviceTransportTypeUSB,
        kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE,
        kAudioDeviceTransportTypeThunderbolt,
    ]
    let microphones = active.filter { device in
        guard let transport = getUInt32Property(device, kAudioDevicePropertyTransportType) else {
            return false
        }
        return physicalTransports.contains(transport) && getInputChannelCount(device) == 1
    }
    let loopbacks = active.filter { device in
        getStringProperty(device, kAudioObjectPropertyName).lowercased() == "blackhole 2ch"
            && getUInt32Property(device, kAudioDevicePropertyTransportType) == kAudioDeviceTransportTypeVirtual
            && getInputChannelCount(device) == 2
    }
    guard microphones.count == 1, let mic = microphones.first,
        loopbacks.count == 1, let loopback = loopbacks.first else {
        return nil
    }
    let micUID = getStringProperty(mic, kAudioDevicePropertyDeviceUID)
    let loopbackUID = getStringProperty(loopback, kAudioDevicePropertyDeviceUID)
    guard !micUID.isEmpty, !loopbackUID.isEmpty, Set(orderedUIDs) == [micUID, loopbackUID] else {
        return nil
    }
    return orderedUIDs.first == micUID ? 0 : 2
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

func jsonStringLiteral(_ value: String) -> String {
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

func renderJSON(statuses: [AudioProcessStatus]) -> String {
    let active = statuses.contains { $0.input || $0.output }
    let processJSON = statuses.map { status in
        """
        {"pid":\(status.pid),"bundle_id":\(jsonStringLiteral(status.bundleID)),"path":\(jsonStringLiteral(status.path)),"input":\(status.input),"output":\(status.output)}
        """
    }.joined(separator: ",")
    return "{\"ok\":true,\"active\":\(active),\"processes\":[\(processJSON)]}"
}

private func wavHeader(rate: UInt32, frames: UInt32) -> Data {
    var data = Data()
    func tag(_ value: String) { data.append(contentsOf: value.utf8) }
    func integer<T: FixedWidthInteger>(_ value: T) {
        var littleEndian = value.littleEndian
        withUnsafeBytes(of: &littleEndian) { data.append(contentsOf: $0) }
    }
    let comment = "recap-microphone-system-v1"
    let size = UInt32(comment.utf8.count + 1)
    let padded = size + size % 2
    tag("RIFF"); integer(36 + 20 + padded + 4 * frames); tag("WAVE")
    tag("fmt "); integer(UInt32(16)); integer(UInt16(1)); integer(UInt16(2))
    integer(rate); integer(4 * rate); integer(UInt16(4)); integer(UInt16(16))
    tag("LIST"); integer(12 + padded); tag("INFO")
    tag("ICMT"); integer(size); tag(comment)
    data.append(contentsOf: repeatElement(0, count: Int(padded - size + 1)))
    tag("data"); integer(4 * frames)
    return data
}

// AudioQueue의 여러 입력 버퍼를 직접 받아 두 채널을 같은 프레임에 쓴다.
// 녹음 스레드만 샘플과 카운터를 쓰며, 호출자는 큐를 멈춘 뒤 헤더를 마감한다.
private final class SplitRecording {
    let output: FileHandle
    let rate: UInt32
    let mic: Int
    var frames: UInt32 = 0
    var failure: Error?
    var micPower = 0.0
    var systemPower = 0.0
    var meterCount = 0
    private var failurePipe: [Int32] = [-1, -1]
    var failureDescriptor: Int32 { failurePipe[0] }

    init(output: FileHandle, rate: UInt32, mic: Int) throws {
        self.output = output
        self.rate = rate
        self.mic = mic
        guard pipe(&failurePipe) == 0 else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
    }

    deinit {
        for fd in failurePipe where fd >= 0 { close(fd) }
    }

    func recordFailure(_ error: Error) {
        // 콜백 스레드만 failure를 쓰며, 메인 스레드는 큐를 멈춘 뒤 읽는다.
        guard failure == nil else { return }
        failure = error
        var signal: UInt8 = 1
        var count: Int
        repeat {
            count = withUnsafePointer(to: &signal) { write(failurePipe[1], $0, 1) }
        } while count < 0 && errno == EINTR
    }

    func finish() throws {
        // 실패 전 완전히 기록한 프레임 수로 헤더를 마감한다. 부분 파일은 삭제하지 않는다.
        do {
            try output.seek(toOffset: 0)
            try output.write(contentsOf: wavHeader(rate: rate, frames: frames))
        } catch {
            try? output.close()
            throw error
        }
        try output.close()
    }

    func append(_ buffer: AudioQueueBufferRef) throws {
        let count = Int(buffer.pointee.mAudioDataByteSize) / 6
        guard count > 0 else { return }
        guard UInt64(frames) + UInt64(count) <= (UInt64(UInt32.max) - 128) / 4 else {
            throw CocoaError(.fileWriteOutOfSpace)
        }
        let input = buffer.pointee.mAudioData.assumingMemoryBound(to: Int16.self)
        let rx = mic == 0 ? 1 : 0
        var stereo = [Int16](repeating: 0, count: count * 2)
        for frame in 0..<count {
            let start = 3 * frame
            let near = input[start + mic]
            let far = Int16((Int32(input[start + rx]) + Int32(input[start + rx + 1])) / 2)
            stereo[2 * frame] = near.littleEndian
            stereo[2 * frame + 1] = far.littleEndian
            micPower += Double(near) * Double(near)
            systemPower += Double(far) * Double(far)
        }
        try stereo.withUnsafeBytes { samples in
            try output.write(contentsOf: Data(samples))
        }
        frames += UInt32(count)
        meterCount += count
        if meterCount >= Int(rate / 4) {
            func level(_ power: Double) -> Double {
                max(-90, 10 * log10(power / (Double(meterCount) * 1_073_741_824)))
            }
            let report = "frame:\(frames)\nlavfi.astats.1.RMS_level=\(level(micPower))\n"
                + "lavfi.astats.2.RMS_level=\(level(systemPower))\n"
            try FileHandle.standardOutput.write(contentsOf: Data(report.utf8))
            meterCount = 0; micPower = 0; systemPower = 0
        }
    }
}

private func waitForRecordingStop(failureDescriptor: Int32) throws {
    var descriptors = [
        pollfd(fd: STDIN_FILENO, events: Int16(POLLIN), revents: 0),
        pollfd(fd: failureDescriptor, events: Int16(POLLIN), revents: 0),
    ]
    while true {
        let status = poll(&descriptors, nfds_t(descriptors.count), -1)
        if status < 0 {
            if errno == EINTR { continue }
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
        if descriptors[1].revents != 0 { return }
        if descriptors[0].revents != 0 {
            var command: UInt8 = 0
            let count = read(STDIN_FILENO, &command, 1)
            if count < 0 && errno == EINTR { continue }
            if count != 1 || command == 113 { return }
        }
    }
}

private func splitRecord(_ name: String, _ path: String, _ rate: UInt32) throws {
    func audioCheck(_ status: OSStatus) throws {
        if status != noErr { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    }
    let devices = try getObjectIDs(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDevices)
    guard (8000...48000).contains(rate), let mic = try aggregateMicChannel(name),
        let device = devices.first(where: { getStringProperty($0, kAudioObjectPropertyName) == name }) else {
        throw CocoaError(.featureUnsupported)
    }
    let fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, S_IRUSR | S_IWUSR)
    guard fd >= 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
    let output = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    try output.write(contentsOf: wavHeader(rate: rate, frames: 0))
    let state = try SplitRecording(output: output, rate: rate, mic: mic)
    var format = AudioStreamBasicDescription(
        mSampleRate: Double(rate), mFormatID: kAudioFormatLinearPCM,
        mFormatFlags: kLinearPCMFormatFlagIsSignedInteger | kLinearPCMFormatFlagIsPacked,
        mBytesPerPacket: 6, mFramesPerPacket: 1, mBytesPerFrame: 6,
        mChannelsPerFrame: 3, mBitsPerChannel: 16, mReserved: 0
    )
    var reference: AudioQueueRef?
    try audioCheck(AudioQueueNewInput(&format, { context, queue, buffer, _, _, _ in
        guard let context else { return }
        let state = Unmanaged<SplitRecording>.fromOpaque(context).takeUnretainedValue()
        guard state.failure == nil else { return }
        do {
            try state.append(buffer)
            let status = AudioQueueEnqueueBuffer(queue, buffer, 0, nil)
            if status != noErr && status != kAudioQueueErr_EnqueueDuringReset {
                state.recordFailure(NSError(domain: NSOSStatusErrorDomain, code: Int(status)))
            }
        } catch { state.recordFailure(error) }
    }, Unmanaged.passUnretained(state).toOpaque(), nil, nil, 0, &reference))
    guard let queue = reference else { throw CocoaError(.featureUnsupported) }
    defer { AudioQueueDispose(queue, true) }
    var uid = getStringProperty(device, kAudioDevicePropertyDeviceUID) as CFString
    try withUnsafePointer(to: &uid) { pointer in
        try audioCheck(AudioQueueSetProperty(queue, kAudioQueueProperty_CurrentDevice, pointer,
                                            UInt32(MemoryLayout<CFString>.size)))
    }
    for _ in 0..<12 {
        var buffer: AudioQueueBufferRef?
        try audioCheck(AudioQueueAllocateBuffer(queue, 6 * (rate / 25), &buffer))
        guard let buffer else { throw CocoaError(.fileWriteUnknown) }
        try audioCheck(AudioQueueEnqueueBuffer(queue, buffer, 0, nil))
    }
    try audioCheck(AudioQueueStart(queue, nil))
    var waitFailure: Error?
    do { try waitForRecordingStop(failureDescriptor: state.failureDescriptor) }
    catch { waitFailure = error }
    let stopStatus = AudioQueueStop(queue, true)
    var finishFailure: Error?
    do { try state.finish() }
    catch { finishFailure = error }
    if let failure = state.failure { throw failure }
    if let failure = waitFailure ?? finishFailure { throw failure }
    try audioCheck(stopStatus)
}

if CommandLine.arguments.count == 5 && CommandLine.arguments[1] == "--record-split",
   let rate = UInt32(CommandLine.arguments[4]) {
    do {
        try splitRecord(CommandLine.arguments[2], CommandLine.arguments[3], rate)
        exit(0)
    } catch {
        fputs("CoreAudio recording: \(error)\n", stderr)
        exit(2)
    }
}

if CommandLine.arguments.contains("--self-test-json") {
    let status = AudioProcessStatus(
        pid: 12345,
        bundleID: "us.zoom.caphost",
        path: "/Applications/zoom.us.app/Contents/Frameworks/CptHost.app/Contents/MacOS/CptHost",
        input: false,
        output: true
    )
    print(renderJSON(statuses: [status]))
    exit(0)
}

if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--aggregate-layout" {
    let channel = try? aggregateMicChannel(CommandLine.arguments[2])
    let value = channel.map { String($0) } ?? "null"
    print("{\"ok\":true,\"mic_channel\":\(value)}")
    exit(0)
}

do {
    let processIDs = try getObjectIDs(
        AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList
    )
    let statuses = processIDs.compactMap { objectID -> AudioProcessStatus? in
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

    print(renderJSON(statuses: statuses))
} catch {
    fputs("{\"ok\":false,\"active\":false,\"error\":\(jsonStringLiteral(String(describing: error)))}\n", stderr)
    exit(2)
}

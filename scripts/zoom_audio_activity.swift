#!/usr/bin/env swift
//
// zoom_audio_activity.swift
//
// CoreAudio process list를 조회해 Zoom 계열 프로세스가 실제 오디오 I/O를
// 실행 중인지 확인한다. 출력은 Python 쪽에서 파싱하기 쉽게 한 줄 JSON이다.

import AppKit
import ApplicationServices
import AudioToolbox
import AVFoundation
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

func getObjectIDs(_ objectID: AudioObjectID, _ selector: AudioObjectPropertySelector,
                  scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) throws -> [AudioObjectID] {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: scope,
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
    return Array(objectIDs.prefix(Int(dataSize) / MemoryLayout<AudioObjectID>.stride))
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

func getChannelCount(_ deviceID: AudioObjectID, _ scope: AudioObjectPropertyScope) -> Int? {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamConfiguration,
        mScope: scope,
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

func getInputChannelCount(_ deviceID: AudioObjectID) -> Int? {
    getChannelCount(deviceID, kAudioObjectPropertyScopeInput)
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
    return bundleID.lowercased().hasPrefix("us.zoom.")
        || path.lowercased().contains("/zoom.us.app/contents/")
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

private enum CaptureStage: String, Encodable {
    case startup
    case inputLayout = "input_layout"
    case inputQueue = "input_queue"
    case writerQueue = "writer_queue"
    case audioConvert = "audio_convert"
    case wavWrite = "wav_write"
    case meterWrite = "meter_write"
    case stopWait = "stop_wait"
    case deviceStop = "device_stop"
    case wavFinalize = "wav_finalize"
}

private enum CaptureProblem: Int, Error {
    case permission = 1, microphone = 2, layout = 3, writerOverrun = 4
}

private struct CaptureFailure: Error, Encodable {
    let stage: CaptureStage
    let errorDomain: String
    let errorCode: Int
    let lastInputFrameMs: Int64?
    var didReport = false

    private enum CodingKeys: String, CodingKey {
        case stage, errorDomain, errorCode, lastInputFrameMs
    }

    init(_ error: Error, stage: CaptureStage, lastInputFrameMs: Int64? = nil) {
        self.stage = stage
        self.lastInputFrameMs = lastInputFrameMs
        if let problem = error as? CaptureProblem {
            errorDomain = "RecapCapture"
            errorCode = problem.rawValue
        } else {
            let native = error as NSError
            switch native.domain {
            case NSOSStatusErrorDomain, NSPOSIXErrorDomain, NSCocoaErrorDomain, AVFoundationErrorDomain:
                errorDomain = native.domain
            default: errorDomain = "other"
            }
            errorCode = native.code
        }
    }
}

@discardableResult
private func writeCaptureDiagnostic(_ failure: CaptureFailure) -> Bool {
    // 원본 오류 설명 대신 고정된 단계와 코드만 전달한다. stderr를 쓰므로 미터 파이프와 독립적이다.
    let encoder = JSONEncoder()
    encoder.keyEncodingStrategy = .convertToSnakeCase
    guard let data = try? encoder.encode(failure) else { return false }
    return fputs("capture-failure=\(String(decoding: data, as: UTF8.self))\n", stderr) >= 0
        && fflush(stderr) == 0
}

private func reportCaptureFailure(_ error: Error) {
    let failure = error as? CaptureFailure ?? CaptureFailure(error, stage: .startup)
    if !failure.didReport { writeCaptureDiagnostic(failure) }
    let problem = failure.errorDomain == "RecapCapture" ? CaptureProblem(rawValue: failure.errorCode) : nil
    switch problem {
    case .permission: print("capture-error=permission")
    case .microphone: print("capture-error=microphone")
    case .layout: print("capture-error=layout")
    default: print("capture-error=coreaudio")
    }
}

// AudioQueue의 여러 입력 버퍼를 직접 받아 두 채널을 같은 프레임에 쓴다.
// 녹음 스레드만 샘플과 카운터를 쓰며, 호출자는 큐를 멈춘 뒤 헤더를 마감한다.
private final class SplitRecording {
    let output: FileHandle
    let rate: UInt32
    let mic: Int
    var frames: UInt32 = 0
    private let diagnosticLock = NSLock()
    private var firstFailure: CaptureFailure?
    private var lastInputFrameMs: Int64?
    var failure: CaptureFailure? { diagnosticLock.withLock { firstFailure } }
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

    func observeInputFrame() {
        let now = Int64(Date().timeIntervalSince1970 * 1000)
        diagnosticLock.withLock {
            if firstFailure == nil { lastInputFrameMs = now }
        }
    }

    private func diagnose(_ error: Error, stage: CaptureStage) -> CaptureFailure {
        CaptureFailure(error, stage: stage, lastInputFrameMs: diagnosticLock.withLock { lastInputFrameMs })
    }

    func recordFailure(_ error: Error, stage: CaptureStage) {
        let recorded = diagnosticLock.withLock {
            guard firstFailure == nil else { return false }
            firstFailure = error as? CaptureFailure ?? CaptureFailure(error, stage: stage, lastInputFrameMs: lastInputFrameMs)
            return true
        }
        guard recorded else { return }
        var signal: UInt8 = 1
        var count: Int
        repeat {
            count = withUnsafePointer(to: &signal) { write(failurePipe[1], $0, 1) }
        } while count < 0 && errno == EINTR
    }

    func recordStatus(_ status: OSStatus, stage: CaptureStage) {
        if status != noErr {
            recordFailure(NSError(domain: NSOSStatusErrorDomain, code: Int(status)), stage: stage)
        }
    }

    func reportFirstFailure() {
        guard let failure, !failure.didReport, writeCaptureDiagnostic(failure) else { return }
        diagnosticLock.withLock { firstFailure?.didReport = true }
    }

    func finish() throws {
        // 실패 전 완전히 기록한 프레임 수로 헤더를 마감한다. 부분 파일은 삭제하지 않는다.
        do {
            try output.seek(toOffset: 0)
            try output.write(contentsOf: wavHeader(rate: rate, frames: frames))
            try output.close()
        } catch {
            try? output.close()
            recordFailure(error, stage: .wavFinalize)
        }
        if let failure { throw failure }
    }

    func append(_ buffer: AudioQueueBufferRef) throws {
        let count = Int(buffer.pointee.mAudioDataByteSize) / 6
        guard count > 0 else { return }
        observeInputFrame()
        try append(buffer.pointee.mAudioData.assumingMemoryBound(to: Int16.self), frames: count)
    }

    func append(_ input: UnsafePointer<Int16>, frames count: Int) throws {
        guard count > 0 else { return }
        guard UInt64(frames) + UInt64(count) <= (UInt64(UInt32.max) - 128) / 4 else {
            throw diagnose(CocoaError(.fileWriteOutOfSpace), stage: .wavWrite)
        }
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
        do {
            try stereo.withUnsafeBytes { samples in
                try output.write(contentsOf: Data(samples))
            }
        } catch {
            throw diagnose(error, stage: .wavWrite)
        }
        frames += UInt32(count)
        meterCount += count
        if meterCount >= Int(rate / 4) {
            func level(_ power: Double) -> Double {
                max(-90, 10 * log10(power / (Double(meterCount) * 1_073_741_824)))
            }
            let report = "frame:\(frames)\nlavfi.astats.1.RMS_level=\(level(micPower))\n"
                + "lavfi.astats.2.RMS_level=\(level(systemPower))\n"
            do { try FileHandle.standardOutput.write(contentsOf: Data(report.utf8)) }
            catch { throw diagnose(error, stage: .meterWrite) }
            meterCount = 0; micPower = 0; systemPower = 0
        }
    }
}

private func waitForRecordingStop(_ recording: SplitRecording) throws {
    // 장치 정지와 WAV 마감이 지연돼도 첫 오류를 제어 스레드에서 먼저 전달한다.
    defer { recording.reportFirstFailure() }
    var descriptors = [
        pollfd(fd: STDIN_FILENO, events: Int16(POLLIN), revents: 0),
        pollfd(fd: recording.failureDescriptor, events: Int16(POLLIN), revents: 0),
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

private func audioCheck(_ status: OSStatus) throws {
    if status != noErr { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
}

private func splitRecord(_ name: String, _ path: String, _ rate: UInt32) throws {
    let devices = try getObjectIDs(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDevices)
    guard (8000...48000).contains(rate), let mic = try aggregateMicChannel(name),
        let device = devices.first(where: { getStringProperty($0, kAudioObjectPropertyName) == name }) else {
        throw CocoaError(.featureUnsupported)
    }
    try recordDevice(device, mic: mic, path: path, rate: rate)
}

private func recordDevice(_ device: AudioObjectID, mic: Int, path: String, rate: UInt32) throws {
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
            if status != kAudioQueueErr_EnqueueDuringReset {
                state.recordStatus(status, stage: .inputQueue)
            }
        } catch { state.recordFailure(error, stage: .wavWrite) }
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
    do { try waitForRecordingStop(state) }
    catch { state.recordFailure(error, stage: .stopWait) }
    state.recordStatus(AudioQueueStop(queue, true), stage: .deviceStop)
    try state.finish()
}

// Zoom의 실제 출력 스트림만 선택한다. 일반 앱 이름에 포함된 "zoom"은 대상이 아니다.
private func zoomAudioObjects() throws -> [AudioObjectID] {
    try getObjectIDs(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList)
        .filter { object in
            let bundle = getStringProperty(object, kAudioProcessPropertyBundleID).lowercased()
            return isZoomProcess(bundleID: bundle, path: getProcessPath(getPIDProperty(object)))
        }.sorted()
}

private struct ListeningDevice {
    let id: AudioObjectID
    let headphones: Bool
    let builtIn: Bool
}

private func listeningDevice(_ devices: [ListeningDevice], preferred: AudioObjectID?) -> AudioObjectID? {
    let current = devices.first { $0.id == preferred }
    if current?.headphones == true { return preferred }
    return devices.first { $0.headphones }?.id ?? current?.id ?? devices.first { $0.builtIn }?.id
}

private func physicalOutputs() throws -> [ListeningDevice] {
    let system = AudioObjectID(kAudioObjectSystemObject)
    return try getObjectIDs(system, kAudioHardwarePropertyDevices).compactMap { id in
        guard let transport = getUInt32Property(id, kAudioDevicePropertyTransportType),
              ![kAudioDeviceTransportTypeVirtual, kAudioDeviceTransportTypeAggregate].contains(transport),
              (getChannelCount(id, kAudioObjectPropertyScopeOutput) ?? 0) > 0 else { return nil }
        let name = getStringProperty(id, kAudioObjectPropertyName).lowercased()
        let streams = try getObjectIDs(id, kAudioDevicePropertyStreams, scope: kAudioObjectPropertyScopeOutput)
        let phones = streams.contains {
            getUInt32Property($0, kAudioStreamPropertyTerminalType) == kAudioStreamTerminalTypeHeadphones
        } || ["earphone", "headphone", "headset", "airpods", "이어폰", "헤드폰", "헤드셋"].contains {
            name.contains($0)
        }
        return ListeningDevice(id: id, headphones: phones, builtIn: transport == kAudioDeviceTransportTypeBuiltIn)
    }
}

// Zoom이 가상 출력이나 다른 물리 출력에 고정된 동안에만 상대 음성을 직접 재생한다.
private final class ZoomMonitor {
    let engine = AVAudioEngine()
    let player = AVAudioPlayerNode()
    let format: AVAudioFormat
    let device: AudioObjectID
    private let slots = DispatchSemaphore(value: 4)

    init(device: AudioObjectID, rate: Double) throws {
        self.device = device
        guard let format = AVAudioFormat(standardFormatWithSampleRate: Double(rate), channels: 2),
              let unit = engine.outputNode.audioUnit else { throw CocoaError(.featureUnsupported) }
        self.format = format
        var output = device
        try audioCheck(AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice,
                                            kAudioUnitScope_Global, 0, &output, UInt32(MemoryLayout.size(ofValue: output))))
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: format)
        try engine.start()
        player.play()
    }

    func play(_ input: AVAudioPCMBuffer) {
        guard let source = input.floatChannelData,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: input.frameLength),
              let pcm = buffer.floatChannelData, slots.wait(timeout: .now()) == .success else { return }
        buffer.frameLength = input.frameLength
        pcm[0].update(from: source[1], count: Int(input.frameLength))
        pcm[1].update(from: source[2], count: Int(input.frameLength))
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [slots] _ in slots.signal() }
    }

    deinit { player.stop(); engine.stop() }
}

@available(macOS 14.2, *)
private final class ZoomTapRoute {
    let tap: AudioObjectID
    let description: CATapDescription
    let rate: Double
    var objects: [AudioObjectID]
    private let lock = NSLock()
    private var monitor: ZoomMonitor?

    init(_ tap: AudioObjectID, _ description: CATapDescription, _ objects: [AudioObjectID], rate: Double) {
        self.tap = tap
        self.description = description
        self.objects = objects
        self.rate = rate
    }

    func play(_ input: AVAudioPCMBuffer) {
        // 장치 재구성 때문에 파일 기록 콜백을 지연시키지 않는다.
        guard lock.try() else { return }
        defer { lock.unlock() }
        monitor?.play(input)
    }

    func refresh() throws {
        let current = try zoomAudioObjects()
        let system = AudioObjectID(kAudioObjectSystemObject)
        let desired = listeningDevice(try physicalOutputs(),
                                      preferred: getUInt32Property(system, kAudioHardwarePropertyDefaultOutputDevice))
        let destinations = Set(try current.flatMap {
            try getObjectIDs($0, kAudioProcessPropertyDevices, scope: kAudioObjectPropertyScopeOutput)
        })
        try update(current: current, destinations: destinations, desired: desired)
    }

    func update(current: [AudioObjectID], destinations: Set<AudioObjectID>, desired: AudioObjectID?) throws {
        lock.lock()
        defer { lock.unlock() }
        let old = monitor
        let relay: AudioObjectID?
        if current.isEmpty || destinations == Set([desired].compactMap { $0 }) {
            relay = nil
        } else if destinations.isEmpty {
            relay = old == nil ? nil : desired
        } else {
            relay = desired
        }
        let next: ZoomMonitor?
        if let device = relay, old?.device != device || old?.engine.isRunning != true {
            // 장치 전환 도중 출력 열기에 실패하면 Zoom의 원래 재생을 즉시 복원한다.
            next = try? ZoomMonitor(device: device, rate: rate)
        } else {
            next = relay == nil ? nil : old
        }
        let mute: CATapMuteBehavior = next == nil ? .unmuted : .mutedWhenTapped
        if current != objects || mute != description.muteBehavior {
            let oldMute = description.muteBehavior
            description.processes = current
            description.muteBehavior = mute
            var value = Unmanaged.passUnretained(description).toOpaque()
            var address = AudioObjectPropertyAddress(mSelector: kAudioTapPropertyDescription,
                mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
            do {
                try audioCheck(AudioObjectSetPropertyData(tap, &address, 0, nil,
                    UInt32(MemoryLayout.size(ofValue: value)), &value))
            } catch {
                description.processes = objects
                description.muteBehavior = oldMute
                throw error
            }
            objects = current
        }
        monitor = next
    }
}

private func tapInputFormat(_ device: AudioObjectID) throws -> AVAudioFormat {
    let streams = try getObjectIDs(device, kAudioDevicePropertyStreams, scope: kAudioObjectPropertyScopeInput)
    var channels: UInt32 = 0
    var rate = 0.0
    for stream in streams {
        var address = AudioObjectPropertyAddress(mSelector: kAudioStreamPropertyVirtualFormat,
            mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout.size(ofValue: asbd))
        try audioCheck(AudioObjectGetPropertyData(stream, &address, 0, nil, &size, &asbd))
        guard asbd.mFormatID == kAudioFormatLinearPCM, asbd.mFormatFlags & kAudioFormatFlagIsFloat != 0,
              asbd.mBitsPerChannel == 32, asbd.mSampleRate > 0,
              rate == 0 || rate == asbd.mSampleRate else { throw CaptureProblem.layout }
        rate = asbd.mSampleRate
        channels += asbd.mChannelsPerFrame
    }
    guard channels == 3, let layout = AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_DiscreteInOrder | channels)
    else { throw CaptureProblem.layout }
    return AVAudioFormat(standardFormatWithSampleRate: rate, channelLayout: layout)
}

// HAL의 버퍼는 콜백 동안만 유효하다. 데이터만 복사하고 변환·디스크 쓰기는 직렬 큐에서 한다.
private func copyTapFrame(_ buffers: UnsafePointer<AudioBufferList>, format: AVAudioFormat) throws -> AVAudioPCMBuffer? {
    let inputs = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffers))
    guard let first = inputs.first, first.mNumberChannels > 0 else { return nil }
    let frames = first.mDataByteSize / (4 * first.mNumberChannels)
    guard frames > 0 else { return nil }
    guard let frame = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames),
          let planes = frame.floatChannelData else { throw CaptureProblem.layout }
    frame.frameLength = frames
    var channel = 0
    for buffer in inputs {
        let n = Int(buffer.mNumberChannels)
        guard n > 0, channel + n <= 3, Int(buffer.mDataByteSize) == Int(frames) * n * 4,
              let samples = buffer.mData?.assumingMemoryBound(to: Float.self) else { throw CaptureProblem.layout }
        for c in 0..<n {
            if n == 1 { planes[channel].update(from: samples, count: Int(frames)) }
            else { for i in 0..<Int(frames) { planes[channel][i] = samples[i * n + c] } }
            channel += 1
        }
    }
    guard channel == 3 else { throw CaptureProblem.layout }
    return frame
}

private func recordTapDevice(_ device: AudioObjectID, format: AVAudioFormat, path: String,
                            rate: UInt32, playback: @escaping (AVAudioPCMBuffer) -> Void) throws {
    guard let layout = format.channelLayout else { throw CaptureProblem.layout }
    let pcm = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Double(rate),
                            interleaved: true, channelLayout: layout)
    guard let converter = AVAudioConverter(from: format, to: pcm) else { throw CaptureProblem.layout }
    let fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, S_IRUSR | S_IWUSR)
    guard fd >= 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
    let file = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    let state = try SplitRecording(output: file, rate: rate, mic: 0)
    try file.write(contentsOf: wavHeader(rate: rate, frames: 0))
    let io = DispatchQueue(label: "recap.zoom.input", qos: .userInteractive)
    let writer = DispatchQueue(label: "recap.zoom.wav", qos: .userInitiated)
    let slots = DispatchSemaphore(value: 32)
    func convert(_ input: AVAudioPCMBuffer?) throws {
        let capacity = ceil(Double(input?.frameLength ?? 1024) * Double(rate) / format.sampleRate) + 1024
        guard let out = AVAudioPCMBuffer(pcmFormat: pcm, frameCapacity: AVAudioFrameCount(capacity))
        else { throw CaptureProblem.layout }
        var supplied = false
        var error: NSError?
        let result = converter.convert(to: out, error: &error) { _, status in
            if let input, !supplied { supplied = true; status.pointee = .haveData; return input }
            status.pointee = input == nil ? .endOfStream : .noDataNow
            return nil
        }
        if let error { throw error }
        guard result != .error, let pointer = out.int16ChannelData else { throw CaptureProblem.layout }
        try state.append(pointer[0], frames: Int(out.frameLength))
    }
    var proc: AudioDeviceIOProcID?
    try audioCheck(AudioDeviceCreateIOProcIDWithBlock(&proc, device, io) { _, buffers, _, _, _ in
        do {
            guard let frame = try copyTapFrame(buffers, format: format) else { return }
            state.observeInputFrame()
            guard slots.wait(timeout: .now()) == .success else {
                writer.async { state.recordFailure(CaptureProblem.writerOverrun, stage: .writerQueue) }
                return
            }
            writer.async {
                defer { slots.signal() }
                guard state.failure == nil else { return }
                do { playback(frame); try convert(frame) }
                catch { state.recordFailure(error, stage: .audioConvert) }
            }
        } catch { writer.async { state.recordFailure(error, stage: .inputLayout) } }
    })
    guard let proc else { throw CaptureProblem.layout }
    defer { AudioDeviceDestroyIOProcID(device, proc) }
    try audioCheck(AudioDeviceStart(device, proc))
    let stop = Result { try waitForRecordingStop(state) }
    let status = AudioDeviceStop(device, proc)
    io.sync {}
    writer.sync {
        // 먼저 대기열의 캡처 오류를 확인한 뒤 정지·마감 과정의 실패를 적용한다.
        if case let .failure(error) = stop { state.recordFailure(error, stage: .stopWait) }
        state.recordStatus(status, stage: .deviceStop)
        if state.failure == nil {
            do { try convert(nil) } catch { state.recordFailure(error, stage: .audioConvert) }
        }
    }
    try state.finish()
}

@available(macOS 14.2, *)
private func recordZoomTap(path: String, rate: UInt32) throws {
    let system = AudioObjectID(kAudioObjectSystemObject)
    let devices = try getObjectIDs(system, kAudioHardwarePropertyDevices)
    // 이어폰의 마이크가 분리되어도 같은 입력 클록을 유지한다.
    guard let mic = devices.first(where: {
        getUInt32Property($0, kAudioDevicePropertyTransportType) == kAudioDeviceTransportTypeBuiltIn
            && getInputChannelCount($0) == 1
    }) else { throw CaptureProblem.microphone }
    let access = DispatchSemaphore(value: 0)
    AVCaptureDevice.requestAccess(for: .audio) { _ in access.signal() }
    access.wait()
    guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else { throw CaptureProblem.permission }
    let objects = try zoomAudioObjects()
    let description = CATapDescription(stereoMixdownOfProcesses: objects)
    description.name = "Recap Zoom"
    description.isPrivate = true
    description.muteBehavior = .unmuted
    var tap = AudioObjectID(kAudioObjectUnknown)
    try audioCheck(AudioHardwareCreateProcessTap(description, &tap))
    defer { AudioHardwareDestroyProcessTap(tap) }
    let micUID = getStringProperty(mic, kAudioDevicePropertyDeviceUID)
    let tapUID = getStringProperty(tap, kAudioTapPropertyUID)
    guard !micUID.isEmpty, !tapUID.isEmpty else { throw CaptureProblem.layout }
    let composition: [String: Any] = [
        kAudioAggregateDeviceUIDKey: UUID().uuidString,
        kAudioAggregateDeviceNameKey: "Recap Zoom Input",
        kAudioAggregateDeviceIsPrivateKey: true,
        kAudioAggregateDeviceMainSubDeviceKey: micUID,
        kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: micUID]],
        kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: tapUID, kAudioSubTapDriftCompensationKey: true]],
    ]
    var aggregate = AudioObjectID(kAudioObjectUnknown)
    try audioCheck(AudioHardwareCreateAggregateDevice(composition as CFDictionary, &aggregate))
    defer { AudioHardwareDestroyAggregateDevice(aggregate) }
    for _ in 0..<50 {
        if getInputChannelCount(aggregate) == 3 { break }
        Thread.sleep(forTimeInterval: 0.02)
    }
    guard getInputChannelCount(aggregate) == 3 else { throw CaptureProblem.layout }
    let format = try tapInputFormat(aggregate)
    let route = ZoomTapRoute(tap, description, objects, rate: format.sampleRate)
    let control = DispatchQueue(label: "recap.zoom.playback")
    let timer = DispatchSource.makeTimerSource(queue: control)
    timer.schedule(deadline: .now(), repeating: .milliseconds(300))
    timer.setEventHandler { do { try route.refresh() } catch { /* 장치 변경 중에는 다음 주기에 재시도 */ } }
    timer.resume()
    defer { timer.cancel(); control.sync {} }
    try recordTapDevice(aggregate, format: format, path: path, rate: rate, playback: route.play)
}

// 회의 창의 명시적 주제 필드만 본다. 창을 열거나 권한을 요청하지 않는다.
private func axAttribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}

private func axText(_ element: AXUIElement, _ name: String) -> String {
    if let text = axAttribute(element, name) as? String, !text.isEmpty {
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    if name == kAXTitleAttribute, let label = axAttribute(element, kAXTitleUIElementAttribute),
       CFGetTypeID(label) == AXUIElementGetTypeID() {
        let labelElement = label as! AXUIElement
        return (axAttribute(labelElement, kAXValueAttribute) as? String) ?? ""
    }
    return ""
}

private func topicLabel(_ value: String, infoPanel: Bool) -> Bool {
    let label = value.trimmingCharacters(in: CharacterSet(charactersIn: " :：")).lowercased()
    return ["meeting topic", "회의 주제"].contains(label)
        || (infoPanel && ["topic", "주제"].contains(label))
}

private func zoomMeetingTopic() -> (String, Bool, Bool) {
    guard AXIsProcessTrusted() else { return ("", false, false) }
    let zoom = NSRunningApplication.runningApplications(withBundleIdentifier: "us.zoom.xos")
    guard zoom.count == 1 else { return ("", zoom.count > 1, true) }
    let app = AXUIElementCreateApplication(zoom[0].processIdentifier)
    AXUIElementSetMessagingTimeout(app, 0.12)
    guard let windows = axAttribute(app, kAXWindowsAttribute) as? [AXUIElement] else { return ("", false, true) }
    let result = extractZoomTopic(windows: windows, text: axText) { node in
        axAttribute(node, kAXChildrenAttribute) as? [AXUIElement] ?? []
    }
    return (result.0, result.1, true)
}

private func extractZoomTopic<Node>(windows: [Node], text: (Node, String) -> String,
                                    childrenOf: (Node) -> [Node]) -> (String, Bool) {
    let meetingNames = ["zoom meeting", "zoom 회의", "zoom webinar", "zoom 웨비나"]
    let infoNames = ["meeting info", "meeting information", "회의 정보"]
    var meetingWindows: [Node] = []
    for window in windows {
        let name = text(window, kAXTitleAttribute).lowercased()
        if meetingNames.contains(name) { meetingWindows.append(window) }
    }
    guard meetingWindows.count == 1 else {
        return ("", meetingWindows.count > 1)
    }
    // 별도 창은 예약된 다른 회의 정보일 수 있다. 실제 회의 창과 그 자식만 허용한다.
    var stack = [(meetingWindows[0], false)]
    var topics = Set<String>()
    var visited = 0
    let deadline = ProcessInfo.processInfo.systemUptime + 1.2
    while let (element, inInfo) = stack.popLast() {
        visited += 1
        guard visited <= 200, ProcessInfo.processInfo.systemUptime < deadline else { return ("", false) }
        let role = text(element, kAXRoleAttribute)
        // 채팅 목록, 문서, 참석자 표, 웹 화면의 내용을 읽지 않는다.
        if ["AXTextField", "AXStaticText"].contains(role) {
            let title = text(element, kAXTitleAttribute)
            let description = text(element, kAXDescriptionAttribute)
            if topicLabel(title, infoPanel: inInfo) || topicLabel(description, infoPanel: inInfo) {
                let value = text(element, kAXValueAttribute)
                if !value.isEmpty && !topicLabel(value, infoPanel: inInfo) { topics.insert(value) }
            }
            continue
        }
        guard ["AXWindow", "AXGroup", "AXSplitGroup", "AXPopover", "AXSheet", "AXToolbar"].contains(role) else { continue }
        let isInfo = inInfo || infoNames.contains(text(element, kAXTitleAttribute).lowercased())
            || infoNames.contains(text(element, kAXDescriptionAttribute).lowercased())
        let children = childrenOf(element)
        if children.count == 2, text(children[0], kAXRoleAttribute) == "AXStaticText",
           topicLabel(text(children[0], kAXValueAttribute), infoPanel: isInfo),
           ["AXStaticText", "AXTextField"].contains(text(children[1], kAXRoleAttribute)) {
            let value = text(children[1], kAXValueAttribute)
            if !value.isEmpty { topics.insert(value) }
        }
        stack.append(contentsOf: children.map { ($0, isInfo) })
    }
    return (topics.count == 1 ? (topics.first ?? "") : "", topics.count > 1)
}

if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "--meeting-topic" {
    let (topic, ambiguous, authorized) = zoomMeetingTopic()
    print("{\"ok\":true,\"topic\":\(jsonStringLiteral(topic)),\"ambiguous\":\(ambiguous),\"authorized\":\(authorized)}")
    exit(0)
}

if CommandLine.arguments.count == 5 && CommandLine.arguments[1] == "--record-split",
   let rate = UInt32(CommandLine.arguments[4]) {
    do {
        try splitRecord(CommandLine.arguments[2], CommandLine.arguments[3], rate)
        exit(0)
    } catch {
        reportCaptureFailure(error)
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

if CommandLine.arguments.count == 4 && CommandLine.arguments[1] == "--record-zoom",
   let rate = UInt32(CommandLine.arguments[3]), (8000...48000).contains(rate) {
    do {
        guard #available(macOS 14.2, *) else { throw CocoaError(.featureUnsupported) }
        try recordZoomTap(path: CommandLine.arguments[2], rate: rate)
        exit(0)
    } catch {
        reportCaptureFailure(error)
        exit(2)
    }
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

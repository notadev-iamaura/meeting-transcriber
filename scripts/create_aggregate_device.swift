//
//  create_aggregate_device.swift
//
//  Meeting Transcriber Aggregate Device 자동 생성 스크립트.
//
//  동작:
//    1. 시스템의 모든 오디오 입력 장치를 순회하며 UID 수집
//    2. 이미 이름에 "Meeting Transcriber Aggregate" 가 포함된 장치가 있으면 SKIP
//    3. 내장 마이크(기본 입력) + BlackHole 2ch 를 찾아 Aggregate Device 로 묶음
//    4. 생성된 장치의 UID 를 stdout 에 출력 (성공 시 "SUCCESS:<UID>")
//       실패 시 "ERROR:<메시지>" + exit code 1
//
//  빌드 & 실행:
//    swiftc scripts/create_aggregate_device.swift -o /tmp/create_aggregate
//    /tmp/create_aggregate
//
//  전제:
//    - macOS + BlackHole 2ch 가 사전 설치되어 있어야 함 (`brew install blackhole-2ch`)
//    - 권한: 마이크 접근 권한 프롬프트가 뜰 수 있으나 허용 없이도 Aggregate 생성은 가능
//
//  주의:
//    - 스크립트는 멱등(idempotent) 하다: 이미 존재하면 생성하지 않고 기존 UID 반환
//    - 생성된 Aggregate 는 재부팅 후에도 유지됨 (~/Library/Preferences/com.apple.audio.DeviceSettings.plist)
//

import CoreAudio
import Foundation

// MARK: - 설정 상수

let AGGREGATE_NAME = "Meeting Transcriber Aggregate"
let AGGREGATE_UID_PREFIX = "com.meeting-transcriber.aggregate"
let BLACKHOLE_NAME_HINT = "BlackHole"

// MARK: - CoreAudio 헬퍼

/// 지정된 속성 주소로부터 문자열 속성을 읽어온다.
func getStringProperty(
    objectID: AudioObjectID,
    selector: AudioObjectPropertySelector
) -> String? {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var size: UInt32 = UInt32(MemoryLayout<CFString?>.size)
    var value: CFString?
    let status = withUnsafeMutablePointer(to: &value) { ptr -> OSStatus in
        return AudioObjectGetPropertyData(objectID, &address, 0, nil, &size, ptr)
    }
    guard status == noErr, let str = value else {
        return nil
    }
    return str as String
}

/// 모든 오디오 디바이스 ID 목록을 반환한다.
func listAllDeviceIDs() -> [AudioObjectID] {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize: UInt32 = 0
    var status = AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &dataSize
    )
    guard status == noErr else { return [] }
    let count = Int(dataSize) / MemoryLayout<AudioObjectID>.size
    var ids = [AudioObjectID](repeating: 0, count: count)
    status = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &dataSize, &ids
    )
    guard status == noErr else { return [] }
    return ids
}

/// 지정된 디바이스가 입력(녹음) 채널을 가지고 있는지 확인한다.
func hasInputStreams(deviceID: AudioObjectID) -> Bool {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamConfiguration,
        mScope: kAudioDevicePropertyScopeInput,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize: UInt32 = 0
    let statusSize = AudioObjectGetPropertyDataSize(deviceID, &address, 0, nil, &dataSize)
    guard statusSize == noErr, dataSize > 0 else { return false }
    let bufferList = UnsafeMutableRawPointer.allocate(byteCount: Int(dataSize), alignment: 16)
    defer { bufferList.deallocate() }
    let status = AudioObjectGetPropertyData(
        deviceID, &address, 0, nil, &dataSize,
        bufferList.assumingMemoryBound(to: AudioBufferList.self)
    )
    guard status == noErr else { return false }
    let list = bufferList.assumingMemoryBound(to: AudioBufferList.self).pointee
    return list.mNumberBuffers > 0
}

/// 기본 입력 장치의 디바이스 ID 를 반환한다.
func getDefaultInputDeviceID() -> AudioObjectID? {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var deviceID: AudioObjectID = 0
    var dataSize: UInt32 = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &dataSize, &deviceID
    )
    guard status == noErr, deviceID != 0 else { return nil }
    return deviceID
}

// MARK: - 메인 로직

/// 이미 존재하는 Aggregate 를 찾아 UID 를 반환한다 (없으면 nil).
func findExistingAggregate() -> String? {
    for id in listAllDeviceIDs() {
        let name = getStringProperty(objectID: id, selector: kAudioObjectPropertyName) ?? ""
        if name.localizedCaseInsensitiveContains(AGGREGATE_NAME) {
            return getStringProperty(objectID: id, selector: kAudioDevicePropertyDeviceUID)
        }
    }
    return nil
}

/// BlackHole 2ch 디바이스 UID 를 반환한다 (없으면 nil).
func findBlackHoleUID() -> String? {
    for id in listAllDeviceIDs() {
        guard hasInputStreams(deviceID: id) else { continue }
        let name = getStringProperty(objectID: id, selector: kAudioObjectPropertyName) ?? ""
        if name.localizedCaseInsensitiveContains(BLACKHOLE_NAME_HINT) {
            return getStringProperty(objectID: id, selector: kAudioDevicePropertyDeviceUID)
        }
    }
    return nil
}

/// 기본 입력 장치의 UID 를 반환한다.
func findDefaultMicUID() -> String? {
    guard let id = getDefaultInputDeviceID() else { return nil }
    return getStringProperty(objectID: id, selector: kAudioDevicePropertyDeviceUID)
}

/// Aggregate Device 를 생성한다. 성공 시 UID 반환.
func createAggregate(micUID: String, blackholeUID: String) -> String? {
    // 타임스탬프를 UID 에 붙여 고유성 확보
    let timestamp = Int(Date().timeIntervalSince1970)
    let aggUID = "\(AGGREGATE_UID_PREFIX).\(timestamp)"

    // 서브 장치 구성: 내장 마이크(drift correction off) + BlackHole(drift correction on)
    let subDevices: [[String: Any]] = [
        [
            kAudioSubDeviceUIDKey as String: micUID,
            kAudioSubDeviceDriftCompensationKey as String: 0,
        ],
        [
            kAudioSubDeviceUIDKey as String: blackholeUID,
            kAudioSubDeviceDriftCompensationKey as String: 1,
        ],
    ]

    let description: [String: Any] = [
        kAudioAggregateDeviceNameKey as String: AGGREGATE_NAME,
        kAudioAggregateDeviceUIDKey as String: aggUID,
        kAudioAggregateDeviceSubDeviceListKey as String: subDevices,
        // 기본 마스터 클럭은 내장 마이크
        kAudioAggregateDeviceMasterSubDeviceKey as String: micUID,
        // private=0 → 시스템 전역에서 선택 가능
        kAudioAggregateDeviceIsPrivateKey as String: 0,
        // stacked=0 → 일반 Aggregate (Multi-Output 은 1)
        kAudioAggregateDeviceIsStackedKey as String: 0,
    ]

    var newDeviceID: AudioObjectID = 0
    let status = AudioHardwareCreateAggregateDevice(
        description as CFDictionary, &newDeviceID
    )
    guard status == noErr, newDeviceID != 0 else {
        FileHandle.standardError.write(
            "AudioHardwareCreateAggregateDevice 실패 (status=\(status))\n".data(using: .utf8)!
        )
        return nil
    }

    return getStringProperty(objectID: newDeviceID, selector: kAudioDevicePropertyDeviceUID)
}

// MARK: - 엔트리 포인트

func main() -> Int32 {
    // 1. 이미 있으면 SKIP
    if let existing = findExistingAggregate() {
        print("SKIP:\(existing)")
        return 0
    }

    // 2. 필요한 원재료 확인
    guard let blackholeUID = findBlackHoleUID() else {
        print("ERROR:BlackHole 2ch 를 찾을 수 없습니다. `brew install blackhole-2ch` 실행 후 재시도")
        return 2
    }
    guard let micUID = findDefaultMicUID() else {
        print("ERROR:기본 입력 장치(내장 마이크) 를 찾을 수 없습니다.")
        return 3
    }

    // 3. 생성
    guard let newUID = createAggregate(micUID: micUID, blackholeUID: blackholeUID) else {
        print("ERROR:Aggregate Device 생성 실패 (CoreAudio API 오류)")
        return 4
    }

    print("SUCCESS:\(newUID)")
    return 0
}

exit(main())

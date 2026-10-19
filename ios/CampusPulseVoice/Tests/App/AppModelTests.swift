import Foundation
import XCTest
@testable import CampusPulseVoice


final class AppTestStore: CredentialStore, @unchecked Sendable {
    private let lock = NSLock()
    private var value: DeviceCredential?
    private var reads = 0
    init(_ value: DeviceCredential?) { self.value = value }
    func load(for origin: BackendOrigin) throws -> DeviceCredential? {
        lock.lock(); defer { lock.unlock() }; reads += 1; return value
    }
    func save(_ credential: DeviceCredential, for origin: BackendOrigin) throws {
        lock.lock(); defer { lock.unlock() }; value = credential
    }
    func remove(for origin: BackendOrigin) throws { lock.lock(); defer { lock.unlock() }; value = nil }
    var loadCount: Int { lock.lock(); defer { lock.unlock() }; return reads }
}

@MainActor final class AppTestConversation: AppConversation {
    let claim: VoiceClaim
    let observer: AppObserver
    let epoch = UUID()
    var onCaptureStatus: (@MainActor (CaptureStatus) -> Void)?
    var onPause: (@MainActor (AudioPauseReason) -> Void)?
    var starts: [Bool] = []
    var stops = 0
    var begins = 0
    var holdStart = false
    var holdBegin = false
    var holdEnd = false
    var holdStop = false
    var startGate: CheckedContinuation<Void, Never>?
    var beginGate: CheckedContinuation<Void, Never>?
    var endGate: CheckedContinuation<Void, Never>?
    var stopGate: CheckedContinuation<Void, Never>?
    private var current: CaptureReady?
    init(claim: VoiceClaim, observer: AppObserver) { self.claim = claim; self.observer = observer }
    func emit(_ state: VoiceUIState = .listening, type: String = "ready") {
        observer.observe(VoiceObservation(state: state, type: type, runID: "run_test", sessionID: nil,
            callID: nil, tool: nil, status: nil, durationMS: nil, elapsedMS: 5, reason: nil), epoch: epoch)
    }
    func start(credential: DeviceCredential, reconnect: Bool) async throws {
        starts.append(reconnect); observer.begin(epoch: epoch)
        if holdStart { await withCheckedContinuation { startGate = $0 } }
        emit()
    }
    func stop() async {
        stops += 1; current = nil; onCaptureStatus?(.inactive)
        startGate?.resume(); startGate = nil
        beginGate?.resume(); beginGate = nil
        endGate?.resume(); endGate = nil
        if holdStop { await withCheckedContinuation { stopGate = $0 } }
    }
    func begin() async throws -> CaptureReady {
        begins += 1
        let ready = CaptureReady(ticket: CaptureTicket(epoch: epoch, connection: 1, utterance: UUID()),
                                 lifetime: UUID(), revision: UUID())
        current = ready; onCaptureStatus?(.arming(ready))
        if holdBegin { await withCheckedContinuation { beginGate = $0 } }
        if current == ready { onCaptureStatus?(.armed(ready)) }
        return ready
    }
    func end() async throws {
        if let current { onCaptureStatus?(.ending(current)) }
        if holdEnd { await withCheckedContinuation { endGate = $0 } }
        current = nil; onCaptureStatus?(.inactive)
    }
    func mute(_ value: Bool) async throws -> CaptureReady? {
        guard let current else { return nil }
        if value { self.current = nil; onCaptureStatus?(.muted(current.ticket)); return nil }
        return nil
    }
    func isCurrent(_ ready: CaptureReady) -> Bool { current == ready && beginGate == nil }
}

@MainActor final class AppModelTests: XCTestCase {
    private let origin = "https://voice.campus.edu"
    private func credential(_ letter: String = "a", expiry: Date = Date().addingTimeInterval(300)) throws -> DeviceCredential {
        try DeviceCredential(token: String(repeating: letter, count: 43), expiresAt: expiry)
    }
    private func waitFor(_ condition: @MainActor () -> Bool) async throws {
        for _ in 0..<500 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        XCTFail("Owned control action did not settle"); throw VoiceFailure.timeout
    }
    func testInitMissingAndExampleOriginsFailBeforeCredentialOrFactory() async throws {
        for value in [nil, "", "https://campuspulse.example.invalid", "http://voice.campus.edu", "https://voice.campus.edu/path"] as [String?] {
            let store = AppTestStore(try credential()); var factories = 0
            let model = VoiceScreenModel(origin: value, store: store, foreground: { true }, factory: { claim, observer in
                factories += 1; return AppTestConversation(claim: claim, observer: observer)
            })
            model.startConversation(); model.pairDevice()
            XCTAssertFalse(model.configured); XCTAssertNotNil(model.message)
            XCTAssertEqual(store.loadCount, 0); XCTAssertEqual(factories, 0)
        }
    }
    func testMissingAndExpiredCredentialFailBeforeAudioCreation() async throws {
        for expired in [false, true] {
            let store = AppTestStore(expired ? try credential(expiry: Date(timeIntervalSince1970: 0)) : nil)
            var factories = 0
            let model = VoiceScreenModel(origin: origin, store: store, foreground: { true }, factory: { claim, observer in
                factories += 1; return AppTestConversation(claim: claim, observer: observer)
            })
            model.startConversation()
            try await waitFor { !model.pending && !model.stopping }
            XCTAssertEqual(factories, 0); XCTAssertFalse(model.wireReady)
            XCTAssertEqual(model.message, AppCopy.error(expired ? AppIssue.credentialExpired : AppIssue.credentialMissing))
        }
    }
    func testStopDuringConnectFencesLateReadyAndJoinsAction() async throws {
        var fake: AppTestConversation?
        let model = VoiceScreenModel(origin: origin, store: AppTestStore(try credential()), foreground: { true }, factory: { claim, observer in
            let value = AppTestConversation(claim: claim, observer: observer); value.holdStart = true; fake = value; return value
        })
        model.startConversation()
        try await waitFor { fake?.startGate != nil }
        XCTAssertEqual(model.connection, .connecting)
        model.requestStop(); model.requestStop()
        XCTAssertFalse(model.speakingPrompt); XCTAssertFalse(model.canStart)
        try await waitFor { !model.stopping }
        fake?.emit(); XCTAssertFalse(model.wireReady); XCTAssertEqual(model.connection, .idle)
    }
    func testStopBeforeQueuedActionRunsPreventsAllStartAndPairingSideEffects() async throws {
        let store = AppTestStore(try credential())
        var factories = 0, pairingFactories = 0
        let model = VoiceScreenModel(origin: origin, store: store, foreground: { true }, factory: { claim, observer in
            factories += 1; return AppTestConversation(claim: claim, observer: observer)
        }, pairingFactory: { origin in
            pairingFactories += 1
            return DevicePairing(origin: origin, http: AppTestPairingHTTP(), store: store, browser: AppTestPairingBrowser())
        })

        model.startConversation(); model.requestStop()
        try await waitFor { !model.stopping }
        XCTAssertEqual(store.loadCount, 0); XCTAssertEqual(factories, 0)
        XCTAssertFalse(model.conversationActive); XCTAssertFalse(model.wireReady)
        XCTAssertEqual(model.connection, .idle); XCTAssertEqual(model.capture, .inactive)
        model.pairDevice(); model.requestStop()
        try await waitFor { !model.stopping }
        XCTAssertEqual(pairingFactories, 0); XCTAssertEqual(store.loadCount, 0)
        XCTAssertNil(model.pairing); XCTAssertEqual(model.connection, .idle)


    }
    func testCaptureAckEndSerializationAndRespondingBargeIn() async throws {
        var fake: AppTestConversation?
        let model = VoiceScreenModel(origin: origin, store: AppTestStore(try credential()), foreground: { true }, factory: { claim, observer in
            let value = AppTestConversation(claim: claim, observer: observer); fake = value; return value
        })
        model.startConversation(); try await waitFor { model.wireReady && !model.pending }
        XCTAssertFalse(model.speakingPrompt)
        fake?.emit(.responding, type: "generation"); XCTAssertTrue(model.canBegin)
        fake?.holdBegin = true; model.beginUtterance()
        try await waitFor { fake?.beginGate != nil }
        XCTAssertFalse(model.speakingPrompt)
        fake?.beginGate?.resume(); fake?.beginGate = nil
        try await waitFor { !model.pending }; XCTAssertTrue(model.speakingPrompt)
        fake?.holdEnd = true; model.endUtterance()
        try await waitFor { fake?.endGate != nil }
        XCTAssertFalse(model.speakingPrompt); XCTAssertFalse(model.canBegin)
        model.beginUtterance(); XCTAssertEqual(fake?.begins, 1)
        fake?.endGate?.resume(); fake?.endGate = nil
        try await waitFor { !model.pending }; XCTAssertTrue(model.canBegin)
        model.requestStop(); try await waitFor { !model.stopping }
    }
    func testStopDuringArmingFencesLateAcknowledgment() async throws {
        var fake: AppTestConversation?
        let model = VoiceScreenModel(origin: origin, store: AppTestStore(try credential()), foreground: { true }, factory: { claim, observer in
            let value = AppTestConversation(claim: claim, observer: observer); value.holdBegin = true; fake = value; return value
        })
        model.startConversation(); try await waitFor { model.wireReady && !model.pending }
        model.beginUtterance(); try await waitFor { fake?.beginGate != nil }
        model.requestStop(); try await waitFor { !model.stopping }
        XCTAssertFalse(model.speakingPrompt); XCTAssertEqual(model.capture, .inactive)
    }
    func testSameClaimRetainsActorNewCredentialWaitsForOldStop() async throws {
        let store = AppTestStore(try credential()); var made: [AppTestConversation] = []
        let model = VoiceScreenModel(origin: origin, store: store, foreground: { true }, factory: { claim, observer in
            let value = AppTestConversation(claim: claim, observer: observer); made.append(value); return value
        })
        model.startConversation(); try await waitFor { model.wireReady && !model.pending }
        model.requestStop(); try await waitFor { !model.stopping }
        model.startConversation(); try await waitFor { model.wireReady && !model.pending }
        XCTAssertEqual(made.count, 1); XCTAssertEqual(made[0].starts, [false, true])
        model.requestStop(); try await waitFor { !model.stopping }
        try store.save(credential("b"), for: BackendOrigin(origin))
        made[0].holdStop = true
        model.startConversation(); try await waitFor { made[0].stopGate != nil }
        XCTAssertEqual(made.count, 1)
        made[0].stopGate?.resume(); made[0].stopGate = nil
        try await waitFor { made.count == 2 && model.wireReady && !model.pending }
        XCTAssertEqual(made[1].starts, [false])
        model.requestStop(); try await waitFor { !model.stopping }
    }
    func testNilUnmuteAndPauseCleanupNeverInventReadinessOrRestart() async throws {
        var fake: AppTestConversation?
        let model = VoiceScreenModel(origin: origin, store: AppTestStore(try credential()), foreground: { true }, factory: { claim, observer in
            let value = AppTestConversation(claim: claim, observer: observer); fake = value; return value
        })
        model.startConversation(); try await waitFor { model.wireReady && !model.pending }
        model.beginUtterance(); try await waitFor { !model.pending }; XCTAssertTrue(model.speakingPrompt)
        model.toggleMute(); try await waitFor { !model.pending }; XCTAssertFalse(model.speakingPrompt)
        model.toggleMute(); try await waitFor { !model.pending }; XCTAssertFalse(model.speakingPrompt)
        let count = fake?.starts.count
        fake?.onPause?(.headphonesRemoved)
        try await waitFor { !model.stopping }
        XCTAssertEqual(fake?.starts.count, count); XCTAssertFalse(model.conversationActive)
        XCTAssertEqual(model.capture, .inactive)
    }
    func testBackgroundCannotStartAndDoesNotStopActiveConversation() async throws {
        var foreground = true; var fake: AppTestConversation?
        let model = VoiceScreenModel(origin: origin, store: AppTestStore(try credential()), foreground: { foreground }, factory: { claim, observer in
            let value = AppTestConversation(claim: claim, observer: observer); fake = value; return value
        })
        model.startConversation(); try await waitFor { model.wireReady && !model.pending }
        foreground = false
        fake?.emit(.responding, type: "generation")
        XCTAssertTrue(model.conversationActive); XCTAssertEqual(fake?.stops, 0)
        model.requestStop(); try await waitFor { !model.stopping }
        let starts = fake?.starts.count
        model.startConversation(); try await waitFor { !model.pending && !model.stopping }
        XCTAssertEqual(fake?.starts.count, starts); XCTAssertEqual(model.message, AppCopy.error(AppIssue.foreground))
    }
}

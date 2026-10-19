import Foundation
import XCTest
@testable import CampusPulseVoice



final class TestArmingGate: @unchecked Sendable {
    private let lock = NSLock()
    private var shouldHold = false
    private var held: PlaybackConsumption?
    private var seen: [CaptureReady] = []
    var identities: [CaptureReady] { lock.lock(); defer { lock.unlock() }; return seen }
    func holdNext() { lock.lock(); shouldHold = true; lock.unlock() }
    private func admit(_ identity: CaptureReady) -> PlaybackConsumption? {
        lock.lock(); defer { lock.unlock() }
        seen.append(identity)
        guard shouldHold else { return nil }
        shouldHold = false; let latch = PlaybackConsumption(); held = latch; return latch
    }
    func enter(_ identity: CaptureReady) async throws {
        if let latch = admit(identity) { try await latch.wait() }
    }
    func release() {
        lock.lock(); let latch = held; held = nil; lock.unlock()
        latch?.resolve(.consumed)
    }
}




actor GatedAudioSocket: VoiceSocket {
    enum Mode: Sendable { case end, muteTrue }
    nonisolated let claim: VoiceClaim
    private let base: FakeVoiceSocket
    private let mode: Mode
    private let gate = PlaybackConsumption()
    private var intercepted = false
    private(set) var waiting = false
    init(mode: Mode) {
        let base = FakeVoiceSocket(); self.base = base; claim = base.claim; self.mode = mode
    }
    func send(_ packet: VoicePacket) async throws {
        try await base.send(packet)
        guard !intercepted, case .text(let data) = packet else { return }
        let message = try WireJSON.parse(data, client: true)
        let match: Bool
        switch mode {
        case .end: match = message.type == "input" && (try? message.text("action")) == "end"
        case .muteTrue: match = message.type == "mute" && (try? message.flag("muted")) == true
        }
        if match {
            intercepted = true; waiting = true; defer { waiting = false }
            try await gate.wait()
        }
    }
    func receive() async throws -> VoicePacket { try await base.receive() }
    func close() async { gate.resolve(.cancelled); await base.close() }
    func release() { gate.resolve(.consumed) }
}

@MainActor private final class CaptureReadyProbe { var value: CaptureReady? }

@MainActor final class CaptureReadinessTests: XCTestCase {
    private func waitFor(_ condition: @MainActor () async -> Bool) async throws {
        for _ in 0..<400 {
            if await condition() { return }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        throw AudioFailure.drainTimeout
    }
    private func compose(socket: any VoiceSocket, gate: TestArmingGate? = nil) throws -> (AudioController, VoiceSession, TestAudioGraph) {
        let graph = TestAudioGraph(events: AudioEventMailbox())
        let audio = AudioController(driver: TestAudioSessionDriver(), beforeArming: { identity in
            if let gate { try await gate.enter(identity) }
        }) { _ in graph }
        let voice = VoiceSession(claim: socket.claim, audio: audio, observer: FakeObserver())
        try audio.bind(voice); return (audio, voice, graph)
    }
    private func connect(_ audio: AudioController, _ voice: VoiceSession, _ socket: any VoiceSocket) async throws {
        try await audio.userStart(); try await voice.start(socket: socket)
        try await waitFor { await voice.state == .listening }
    }
    func testDelayedRealWireBeginDoesNotReturnReadyUntilNativeTapArms() async throws {
        let socket = GatedAudioSocket(mode: .end)
        let (audio, voice, graph) = try compose(socket: socket)
        try await connect(audio, voice, socket)
        let first = try await audio.userBeginUtterance()
        XCTAssertTrue(audio.isCurrent(first)); XCTAssertEqual(graph.starts, 1)
        try graph.feed(frames: 137); try await audio.userEndUtterance()
        try await waitFor { await socket.waiting }
        let probe = CaptureReadyProbe()
        let begin = Task { let ready = try await audio.userBeginUtterance(); probe.value = ready; return ready }
        try await waitFor { if case .arming = audio.captureStatus { return true }; return false }
        let before = await voice.machine
        XCTAssertEqual(before.wire?.inputOpen, false); XCTAssertNil(probe.value); XCTAssertEqual(graph.starts, 1)
        await socket.release()
        let ready = try await begin.value
        XCTAssertTrue(audio.isCurrent(ready)); XCTAssertEqual(graph.starts, 2)
        XCTAssertNotEqual(ready.ticket, first.ticket)
        await audio.userStop(); XCTAssertFalse(audio.isCurrent(ready))
    }
    func testUnmuteReadyWaitsForRealWireAdmissionAndNeverReopensEndedInput() async throws {
        let socket = GatedAudioSocket(mode: .muteTrue)
        let (audio, voice, graph) = try compose(socket: socket)
        try await connect(audio, voice, socket)
        let old = try await audio.userBeginUtterance()
        let muted = try await audio.userMute(true); XCTAssertNil(muted); XCTAssertFalse(audio.isCurrent(old))
        try await waitFor { await socket.waiting }
        let probe = CaptureReadyProbe()
        let unmute = Task { let ready = try await audio.userMute(false); probe.value = ready; return ready }
        try await waitFor { if case .arming = audio.captureStatus { return true }; return false }
        let before = await voice.machine
        XCTAssertEqual(before.wire?.muted, true); XCTAssertNil(probe.value); XCTAssertEqual(graph.starts, 1)
        await socket.release()
        let unmuted = try await unmute.value
        let ready = try XCTUnwrap(unmuted)
        XCTAssertTrue(audio.isCurrent(ready)); XCTAssertEqual(ready.ticket, old.ticket); XCTAssertNotEqual(ready.revision, old.revision)
        let repeated = try await audio.userMute(false)
        XCTAssertEqual(repeated, ready); XCTAssertEqual(graph.starts, 2)
        try graph.feed(frames: 137); try await audio.userEndUtterance()
        let noCapture = try await audio.userMute(false); XCTAssertNil(noCapture)
        let ended = await voice.machine; XCTAssertEqual(ended.wire?.inputOpen, false); XCTAssertEqual(graph.starts, 2)
        await audio.userStop()
    }
    func testFailedNativeArmFailsAcknowledgmentAndNeverPromptsArmed() async throws {
        let socket = FakeVoiceSocket()
        let (audio, voice, graph) = try compose(socket: socket)
        graph.failArm = true
        var armed: [CaptureReady] = []
        audio.onCaptureStatus = { if case .armed(let ready) = $0 { armed.append(ready) } }
        try await connect(audio, voice, socket)
        do { _ = try await audio.userBeginUtterance(); XCTFail("failed arm returned ready") }
        catch { XCTAssertEqual(error as? AudioFailure, .graph) }
        XCTAssertTrue(armed.isEmpty); XCTAssertEqual(graph.armAttempts, 1); XCTAssertEqual(graph.starts, 0)
        await audio.userStop()
    }
    func testStopDuringArmingCancelsAckAndLateGateCannotArmFreshLifetime() async throws {
        let gate = TestArmingGate(); gate.holdNext()
        var made: [TestAudioGraph] = []
        let audio = AudioController(driver: TestAudioSessionDriver(), beforeArming: { try await gate.enter($0) }) { events in
            let graph = TestAudioGraph(events: events); made.append(graph); return graph
        }
        let socket = FakeVoiceSocket(), voice = VoiceSession(claim: socket.claim, audio: audio, observer: FakeObserver())
        try audio.bind(voice)
        try await connect(audio, voice, socket)
        let begin = Task { try await audio.userBeginUtterance() }
        try await waitFor { gate.identities.count == 1 }
        let retired = try XCTUnwrap(gate.identities.first)
        await audio.userStop()
        do { _ = try await begin.value; XCTFail("stopped arm returned ready") } catch {}
        gate.release()
        XCTAssertEqual(made[0].starts, 0); XCTAssertFalse(audio.isCurrent(retired)); XCTAssertEqual(audio.captureStatus, .inactive)
        try await audio.userStart()
        let fresh = FakeVoiceSocket(claim: socket.claim, connection: 2)
        try await voice.start(socket: fresh, reconnect: true)
        try await waitFor { await voice.state == .listening }
        let ready = try await audio.userBeginUtterance()
        gate.release()
        XCTAssertTrue(audio.isCurrent(ready)); XCTAssertFalse(audio.isCurrent(retired))
        XCTAssertEqual(made[0].starts, 0); XCTAssertEqual(made[1].starts, 1)
        await audio.userStop()
    }
    func testCallerCancellationCancelsPendingAckAndStopsCaptureWithoutLateReady() async throws {
        let gate = TestArmingGate(); gate.holdNext()
        let socket = FakeVoiceSocket()
        let (audio, voice, graph) = try compose(socket: socket, gate: gate)
        try await connect(audio, voice, socket)
        let begin = Task { try await audio.userBeginUtterance() }
        try await waitFor { gate.identities.count == 1 }
        begin.cancel()
        do { _ = try await begin.value; XCTFail("cancelled arming returned ready") }
        catch { XCTAssertEqual(error as? AudioFailure, .armingCancelled) }
        gate.release(); await audio.userStop()
        XCTAssertEqual(graph.starts, 0); XCTAssertEqual(audio.captureStatus, .inactive)
    }
    func testMuteRetiresPendingRevisionAndStaleGateCannotPublishItAsReady() async throws {
        let gate = TestArmingGate(); gate.holdNext()
        let socket = FakeVoiceSocket()
        let (audio, voice, graph) = try compose(socket: socket, gate: gate)
        try await connect(audio, voice, socket)
        let oldBegin = Task { try await audio.userBeginUtterance() }
        try await waitFor { gate.identities.count == 1 }
        let old = try XCTUnwrap(gate.identities.first)
        try await audio.userMute(true)
        do { _ = try await oldBegin.value; XCTFail("muted revision returned ready") } catch {}
        let unmute = Task { try await audio.userMute(false) }
        try await waitFor { if case .arming(let pending) = audio.captureStatus { return pending.revision != old.revision }; return false }
        gate.release()
        let unmuted = try await unmute.value
        let ready = try XCTUnwrap(unmuted)
        XCTAssertNotEqual(ready.revision, old.revision); XCTAssertEqual(ready.ticket, old.ticket)
        XCTAssertTrue(audio.isCurrent(ready)); XCTAssertFalse(audio.isCurrent(old)); XCTAssertEqual(graph.starts, 1)
        await audio.userStop()
    }
    func testArmingDeadlineFailsBoundedAckWithoutNativeStart() async throws {
        let gate = TestArmingGate(); gate.holdNext()
        let socket = FakeVoiceSocket()
        let (audio, voice, graph) = try compose(socket: socket, gate: gate)
        try await connect(audio, voice, socket)
        do { _ = try await audio.userBeginUtterance(); XCTFail("unreleased admission returned ready") }
        catch { XCTAssertEqual(error as? AudioFailure, .admissionTimeout) }
        gate.release(); await audio.userStop()
        XCTAssertEqual(graph.starts, 0)
    }
    func testRetiredAckCannotAcceptForeignOrLateSuccessfulRevision() async throws {
        let ticket = CaptureTicket(epoch: UUID(), connection: 1, utterance: UUID())
        let old = CaptureReady(ticket: ticket, lifetime: UUID(), revision: UUID()), ack = CaptureArming(old)
        let other = CaptureReady(ticket: ticket, lifetime: old.lifetime, revision: UUID())
        XCTAssertFalse(ack.resolve(.success(other)))
        XCTAssertTrue(ack.resolve(.failure(.retiredTicket)))
        XCTAssertFalse(ack.resolve(.success(old)))
        do { _ = try await ack.wait(); XCTFail("retired ack returned success") }
        catch { XCTAssertEqual(error as? AudioFailure, .retiredTicket) }
    }
}

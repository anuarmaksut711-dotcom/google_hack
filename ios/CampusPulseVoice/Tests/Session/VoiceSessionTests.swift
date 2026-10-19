import XCTest
@testable import CampusPulseVoice

enum TestClaim {
    static func make(token: String = String(repeating: "c", count: 43)) -> VoiceClaim {
        let origin = try! BackendOrigin("https://voice.example.invalid")
        return VoiceClaim(origin: origin, credential: try! DeviceCredential(token: token, expiresAt: Date().addingTimeInterval(300)))
    }
}

actor FakeVoiceSocket: VoiceSocket {
    nonisolated let claim: VoiceClaim
    private let connection: UInt64
    private let autoReady: Bool
    private let delayCloseReceive: Bool
    init(claim: VoiceClaim = TestClaim.make(), connection: UInt64 = 1, autoReady: Bool = true, delayCloseReceive: Bool = false) {
        self.claim = claim; self.connection = connection; self.autoReady = autoReady; self.delayCloseReceive = delayCloseReceive
    }
    private var queue: [VoicePacket] = []
    private var waiter: CheckedContinuation<VoicePacket, Error>?
    private(set) var sent: [VoicePacket] = []
    private(set) var closed = false
    private var sequence: UInt64 = 0
    var failPCM = false
    var holdPCM = false
    private(set) var activeSends = 0
    func configure(failPCM: Bool = false, holdPCM: Bool = false) { self.failPCM = failPCM; self.holdPCM = holdPCM }
    func inject(_ type: String, _ fields: [String: Any] = [:]) throws {
        let packet: VoicePacket = .text(try TestEvent.data(type, sequence: sequence, connection: connection, fields)); sequence += 1
        push(packet)
    }
    func push(_ packet: VoicePacket) {
        guard !closed else { return }
        if let waiter { self.waiter = nil; waiter.resume(returning: packet) }
        else { precondition(queue.count < 32); queue.append(packet) }
    }
    func send(_ packet: VoicePacket) async throws {
        guard !closed else { throw VoiceFailure.transport }
        activeSends += 1; defer { activeSends -= 1 }
        sent.append(packet)
        if case .binary = packet {
            if holdPCM { try await Task.sleep(nanoseconds: 10_000_000_000) }
            if failPCM { throw VoiceFailure.transport }
        }
        if case .text(let data) = packet {
            let m = try WireJSON.parse(data, client: true)
            if m.type == "start" {
                if autoReady { sequence = 1; push(.text(try TestEvent.ready(connection: connection))) }
            } else if m.type == "stop" { try inject("closed", ["reason": "stopped"]) }
        }
    }
    func receive() async throws -> VoicePacket {
        if !queue.isEmpty { return queue.removeFirst() }
        guard !closed else { throw VoiceFailure.transport }
        return try await withCheckedThrowingContinuation { waiter = $0 }
    }
    func close() async {
        closed = true; queue.removeAll()
        let pending = waiter; waiter = nil
        // Deterministically allow the session watch child to finish normally
        // before the pending receive wakes during a pre-Ready local Stop.
        if delayCloseReceive { try? await Task.sleep(nanoseconds: 100_000_000) }
        pending?.resume(throwing: VoiceFailure.transport)
    }
    var pendingReceive: Bool { waiter != nil }
}

@MainActor final class FakeAudio: VoiceAudioPort {
    var captures: [CaptureTicket] = []
    var stopped: [CaptureTicket] = []
    var played: [PlaybackTicket] = []
    var admissions: [PlaybackAdmission] = []
    var scheduled: [UUID: PlaybackReceipt] = [:]
    var allReceipts: [UUID: PlaybackReceipt] = [:]
    var flushed: [UInt64] = []
    var holdPlayback = false
    var failScheduleSequence: UInt32?
    var beforeSchedule: ((PlaybackAdmission) throws -> Void)?
    private var closedEpochs: Set<UUID> = []
    private var minimumSerial: [UUID: UInt64] = [:]
    func startCapture(ticket: CaptureTicket) throws {
        guard !closedEpochs.contains(ticket.epoch), !stopped.contains(ticket) else { throw VoiceFailure.invalidState }
        captures.append(ticket)
    }
    func stopCapture(ticket: CaptureTicket) { stopped.append(ticket) }
    func muteCapture(_ muted: Bool, ticket: CaptureTicket) {}
    func scheduleOutput(_ frame: AudioEnvelope, admission: PlaybackAdmission) throws -> PlaybackReceipt {
        let ticket = admission.ticket
        guard !closedEpochs.contains(ticket.epoch), ticket.serial >= minimumSerial[ticket.epoch, default: 0],
              frame.connection == admission.connection, frame.sequence == admission.sequence,
              frame.generation == ticket.generation, frame.pcm.count == admission.pcmBytes else { throw CancellationError() }
        try beforeSchedule?(admission)
        played.append(ticket); admissions.append(admission)
        let receipt = PlaybackReceipt(admission: admission, consumption: PlaybackConsumption())
        scheduled[admission.id] = receipt; allReceipts[admission.id] = receipt
        // Deliberately throw AFTER fake native enqueue to test uncertain schedule.
        if frame.sequence == failScheduleSequence { throw VoiceFailure.transport }
        if !holdPlayback { completeNative(admission.id, outcome: .consumed) }
        return receipt
    }
    func completeNative(_ id: UUID, outcome: PlaybackOutcome) {
        if outcome != .failed { scheduled.removeValue(forKey: id) }
        allReceipts[id]?.consumption.resolve(outcome)
    }
    func flushOutput(epoch: UUID, nextSerial: UInt64) {
        flushed.append(nextSerial)
        minimumSerial[epoch] = max(minimumSerial[epoch, default: 0], nextSerial)
        let cancelled = scheduled.values.filter { $0.admission.ticket.epoch == epoch && $0.admission.ticket.serial < nextSerial }
        // Fake synchronous native unschedule occurs BEFORE cancellation resolution.
        for receipt in cancelled { scheduled.removeValue(forKey: receipt.admission.id) }
        cancelled.forEach { $0.consumption.resolve(.cancelled) }
    }
    func close(epoch: UUID) { closedEpochs.insert(epoch); flushOutput(epoch: epoch, nextSerial: UInt64.max) }
}

@MainActor final class FakeObserver: VoiceObserver {
    var observations: [VoiceObservation] = []
    var transcripts: [String: WireState.Transcript] = [:]
    private var epoch: UUID?
    private var dataOpen = false
    func begin(epoch: UUID) { self.epoch = epoch; dataOpen = true; transcripts.removeAll() }
    func observe(_ observation: VoiceObservation, epoch: UUID) {
        guard epoch == self.epoch, dataOpen || ["closed", "error"].contains(observation.type) else { return }
        observations.append(observation)
        if observations.count > 32 { observations.removeFirst() }
    }
    func replaceTranscript(id: String, value: WireState.Transcript, epoch: UUID) {
        guard epoch == self.epoch, dataOpen else { return }; transcripts[id] = value
    }
    func suspendData(epoch: UUID) { if self.epoch == epoch { dataOpen = false } }
    func close(epoch: UUID) { suspendData(epoch: epoch) }
}

@MainActor final class VoiceSessionTests: XCTestCase {
    private func eventually(_ condition: @escaping @Sendable () async -> Bool) async throws {
        try await bounded(seconds: 3) {
            while !(await condition()) { try await Task.sleep(nanoseconds: 5_000_000) }
        }
    }
    private func pushOutput(_ socket: FakeVoiceSocket, sequence: UInt32, generation: UInt32 = 1, bytes: Int = 960) async throws {
        let frame = AudioEnvelope(kind: .output, connection: 1, generation: generation, sequence: sequence, pcm: Data(repeating: 0, count: bytes))
        await socket.push(.binary(try frame.encode()))
    }
    func testOrderedPipelineAdmitsThreeBeforeAnyNativeConsumptionAndStopJoinsAll() async throws {
        let socket = FakeVoiceSocket(), audio = FakeAudio(), observer = FakeObserver()
        audio.holdPlayback = true
        let session = VoiceSession(claim: socket.claim, audio: audio, observer: observer)
        try await session.start(socket: socket)
        try await eventually { await session.state == .listening }
        try await socket.inject("generation", ["generation": 1, "phase": "started"])
        for sequence in 0..<3 { try await pushOutput(socket, sequence: UInt32(sequence)) }
        try await eventually { await MainActor.run { audio.admissions.count == 3 } }
        XCTAssertEqual(audio.admissions.map(\.sequence), [0, 1, 2])
        XCTAssertEqual(audio.scheduled.count, 3)
        XCTAssertTrue(audio.allReceipts.values.allSatisfy { $0.consumption.resolvedOutcome == nil })
        let pending = await session.playbackUsage
        XCTAssertEqual(pending.bytes, 2880); XCTAssertEqual(pending.packets, 3)
        let first = audio.admissions[0]
        audio.completeNative(first.id, outcome: .consumed)
        try await eventually { await session.playbackUsage.bytes == 1920 }
        await session.stop()
        let final = await session.playbackUsage
        XCTAssertEqual(final.bytes, 0); XCTAssertEqual(final.packets, 0); XCTAssertTrue(audio.scheduled.isEmpty)
        for admission in audio.admissions.dropFirst() {
            audio.completeNative(admission.id, outcome: .consumed) // late native callbacks
            XCTAssertEqual(audio.allReceipts[admission.id]?.consumption.resolvedOutcome, .cancelled)
        }
        let active = await socket.activeSends, receive = await socket.pendingReceive
        XCTAssertEqual(active, 0); XCTAssertFalse(receive)
    }
    func testInterruptionFlushesEveryOutstandingReceiptAndNextGenerationIsOrdered() async throws {
        let socket = FakeVoiceSocket(), audio = FakeAudio(), observer = FakeObserver()
        audio.holdPlayback = true
        let session = VoiceSession(claim: socket.claim, audio: audio, observer: observer)
        try await session.start(socket: socket)
        try await eventually { await session.state == .listening }
        try await socket.inject("generation", ["generation": 1, "phase": "started"])
        for sequence in 0..<2 { try await pushOutput(socket, sequence: UInt32(sequence), bytes: 2) }
        try await eventually { await MainActor.run { audio.scheduled.count == 2 } }
        let old = audio.admissions
        try await socket.inject("interrupted", ["generation": 1])
        try await eventually { await session.playbackUsage.packets == 0 }
        XCTAssertTrue(audio.scheduled.isEmpty)
        try await socket.inject("generation", ["generation": 2, "phase": "started"])
        for sequence in 0..<2 { try await pushOutput(socket, sequence: UInt32(sequence), generation: 2, bytes: 2) }
        try await eventually { await MainActor.run { audio.admissions.count == 4 } }
        XCTAssertEqual(audio.admissions.map(\.sequence), [0, 1, 0, 1])
        XCTAssertEqual(audio.admissions.map { $0.ticket.generation }, [1, 1, 2, 2])
        XCTAssertGreaterThan(audio.admissions[2].ticket.serial, old[0].ticket.serial)
        for admission in old {
            audio.completeNative(admission.id, outcome: .consumed)
            XCTAssertEqual(audio.allReceipts[admission.id]?.consumption.resolvedOutcome, .cancelled)
        }
        let pending = await session.playbackUsage
        XCTAssertEqual(pending.bytes, 4); XCTAssertEqual(pending.packets, 2)
        await session.stop()
    }
    func testSchedulingFailureAfterNativeEnqueueFlushesUncertainPCMAndPendingReceipts() async throws {
        let socket = FakeVoiceSocket(), audio = FakeAudio(), observer = FakeObserver()
        audio.holdPlayback = true; audio.failScheduleSequence = 1
        let session = VoiceSession(claim: socket.claim, audio: audio, observer: observer)
        try await session.start(socket: socket)
        try await eventually { await session.state == .listening }
        try await socket.inject("generation", ["generation": 1, "phase": "started"])
        try await pushOutput(socket, sequence: 0); try await pushOutput(socket, sequence: 1); try await pushOutput(socket, sequence: 2)
        try await eventually { await socket.closed }; await session.stop()
        let state = await session.state, pending = await session.playbackUsage
        XCTAssertEqual(state, .error); XCTAssertEqual(pending.bytes, 0); XCTAssertEqual(pending.packets, 0)
        XCTAssertEqual(audio.admissions.map(\.sequence), [0, 1]); XCTAssertTrue(audio.scheduled.isEmpty)
        XCTAssertTrue(audio.allReceipts.values.allSatisfy { $0.consumption.resolvedOutcome == .cancelled })
    }
    func testConsumptionFailureIsNotSuccessAndFlushesOtherScheduledBuffers() async throws {
        let socket = FakeVoiceSocket(), audio = FakeAudio(), observer = FakeObserver()
        audio.holdPlayback = true
        let session = VoiceSession(claim: socket.claim, audio: audio, observer: observer)
        try await session.start(socket: socket)
        try await eventually { await session.state == .listening }
        try await socket.inject("generation", ["generation": 1, "phase": "started"])
        try await pushOutput(socket, sequence: 0); try await pushOutput(socket, sequence: 1)
        try await eventually { await MainActor.run { audio.scheduled.count == 2 } }
        let first = audio.admissions[0], second = audio.admissions[1]
        audio.completeNative(first.id, outcome: .failed)
        try await eventually { await socket.closed }; await session.stop()
        XCTAssertEqual(audio.allReceipts[first.id]?.consumption.resolvedOutcome, .failed)
        XCTAssertEqual(audio.allReceipts[second.id]?.consumption.resolvedOutcome, .cancelled)
        XCTAssertTrue(audio.scheduled.isEmpty)
        let state = await session.state, usage = await session.playbackUsage
        XCTAssertEqual(state, .error); XCTAssertEqual(usage.bytes, 0); XCTAssertEqual(usage.packets, 0)
    }
    func testPreReadyStopWatchFirstCleanupJoinsThenAllowsRestart() async throws {
        let claim = TestClaim.make()
        let socket = FakeVoiceSocket(claim: claim, autoReady: false)
        // Use exactly the same immutable claim, including expiry, for all sockets.
        let pending = FakeVoiceSocket(claim: claim, autoReady: false, delayCloseReceive: true)
        let session = VoiceSession(claim: claim, audio: FakeAudio(), observer: FakeObserver())
        try await session.start(socket: pending)
        try await eventually { await pending.pendingReceive }
        let stopper = Task { await session.stop() }
        try await eventually { await session.state == .stopping }
        do { try await session.start(socket: socket); XCTFail("restart before cleanup") } catch {}
        await stopper.value
        let state = await session.state; XCTAssertEqual(state, .idle)
        let fresh = FakeVoiceSocket(claim: claim)
        try await session.start(socket: fresh)
        try await eventually { await session.state == .listening }
        await session.stop()
    }
    func testNewCredentialMayStartAtOneSameCredentialRejectsStaleAndWrongClaim() async throws {
        let oldClaim = TestClaim.make(), newClaim = TestClaim.make(token: String(repeating: "d", count: 43))
        let old = VoiceSession(claim: oldClaim, audio: FakeAudio(), observer: FakeObserver())
        let first = FakeVoiceSocket(claim: oldClaim)
        try await old.start(socket: first)
        try await eventually { await old.state == .listening }; await old.stop()
        let wrong = FakeVoiceSocket(claim: newClaim)
        do { try await old.start(socket: wrong); XCTFail("claim adoption must require new actor") } catch {}
        let stale = FakeVoiceSocket(claim: oldClaim)
        try await old.start(socket: stale, reconnect: true)
        try await eventually { await stale.closed }; await old.stop()
        let oldState = await old.state; XCTAssertEqual(oldState, .error)
        let adopted = VoiceSession(claim: newClaim, audio: FakeAudio(), observer: FakeObserver())
        let fresh = FakeVoiceSocket(claim: newClaim)
        try await adopted.start(socket: fresh)
        try await eventually { await adopted.state == .listening }; await adopted.stop()
    }
    func testFakeDuplexExplicitTurnsUniqueCaptureTicketsAndJoinedStop() async throws {
        let socket = FakeVoiceSocket(), audio = FakeAudio(), observer = FakeObserver()
        let session = VoiceSession(claim: socket.claim, audio: audio, observer: observer)
        try await session.start(socket: socket)
        try await eventually { await session.state == .listening }
        try await socket.inject("generation", ["generation": 1, "phase": "started"])
        try await eventually { await session.state == .responding }
        let first = try await session.beginUtterance()
        try await session.submitPCM(Data(repeating: 0, count: 640), ticket: first)
        try await session.endUtterance()
        try await eventually { await session.machine.wire?.inputOpen == false }
        let second = try await session.beginUtterance()
        XCTAssertNotEqual(first, second)
        do { try await session.submitPCM(Data([0, 0]), ticket: first); XCTFail("old callback accepted") } catch {}
        try await session.submitPCM(Data(repeating: 0, count: 640), ticket: second)
        await session.stop(); await session.stop()
        let closed = await socket.closed, sends = await socket.activeSends, pending = await socket.pendingReceive
        XCTAssertTrue(closed); XCTAssertEqual(sends, 0); XCTAssertFalse(pending)
        XCTAssertFalse(audio.stopped.isEmpty); XCTAssertTrue(audio.scheduled.isEmpty)
        let packets = await socket.sent
        let frames = try packets.compactMap { packet -> AudioEnvelope? in
            if case .binary(let data) = packet { return try AudioEnvelope.decode(data, expected: .input) }; return nil
        }
        XCTAssertEqual(frames.first?.generation, 0)
    }
    func testInterruptionFlushesAlreadyScheduledPlaybackAndFinalUserStops() async throws {
        let socket = FakeVoiceSocket(), audio = FakeAudio(), observer = FakeObserver()
        audio.holdPlayback = true
        let session = VoiceSession(claim: socket.claim, audio: audio, observer: observer)
        try await session.start(socket: socket)
        try await eventually { await session.state == .listening }
        try await socket.inject("generation", ["generation": 1, "phase": "started"])
        await socket.push(.binary(try AudioEnvelope(kind: .output, connection: 1, generation: 1, sequence: 0, pcm: Data([0, 0])).encode()))
        try await eventually { await MainActor.run { audio.scheduled.count == 1 } }
        try await socket.inject("interrupted", ["generation": 1])
        try await eventually { await MainActor.run { audio.scheduled.isEmpty } }
        try await socket.inject("transcript", ["generation": 0, "transcript_id": "user", "speaker": "user", "revision": 0, "final": true, "text": "закончить разговор"])
        try await eventually { await socket.closed }
        await session.stop()
        XCTAssertTrue(audio.scheduled.isEmpty)
        XCTAssertNil(observer.transcripts["user"]) // command is control only after validation
    }
    func testUncertainAdmittedSendFailsConnectionWithoutReplay() async throws {
        let socket = FakeVoiceSocket(), audio = FakeAudio(), observer = FakeObserver()
        await socket.configure(failPCM: true)
        let session = VoiceSession(claim: socket.claim, audio: audio, observer: observer)
        try await session.start(socket: socket)
        try await eventually { await session.state == .listening }
        let ticket = try await session.beginUtterance()
        try await session.submitPCM(Data([0, 0]), ticket: ticket)
        try await eventually { await socket.closed }
        await session.stop()
        let packets = await socket.sent
        XCTAssertEqual(packets.filter { if case .binary = $0 { return true }; return false }.count, 1)
        let pending = await socket.pendingReceive, active = await socket.activeSends
        XCTAssertFalse(pending); XCTAssertEqual(active, 0)
        do { try await session.submitPCM(Data([0, 0]), ticket: ticket); XCTFail("uncertain connection reused") } catch {}
    }
    func testStopDuringAdmittedBoundedSendJoinsTimeoutWithoutReusingSocket() async throws {
        let socket = FakeVoiceSocket(), audio = FakeAudio(), observer = FakeObserver()
        await socket.configure(holdPCM: true)
        let session = VoiceSession(claim: socket.claim, audio: audio, observer: observer)
        try await session.start(socket: socket)
        try await eventually { await session.state == .listening }
        let ticket = try await session.beginUtterance()
        try await session.submitPCM(Data([0, 0]), ticket: ticket)
        try await eventually { await socket.activeSends == 1 }
        await session.stop()
        let closed = await socket.closed, active = await socket.activeSends
        XCTAssertTrue(closed); XCTAssertEqual(active, 0)
        let packets = await socket.sent
        XCTAssertEqual(packets.filter { if case .binary = $0 { return true }; return false }.count, 1)
    }
    func testMuteDoesNotReopenEndedInputAndExplicitUnmuteKeepsSequence() async throws {
        let socket = FakeVoiceSocket(), audio = FakeAudio(), observer = FakeObserver()
        let session = VoiceSession(claim: socket.claim, audio: audio, observer: observer)
        try await session.start(socket: socket)
        try await eventually { await session.state == .listening }
        let ticket = try await session.beginUtterance()
        try await session.setMuted(true)
        try await eventually { await session.machine.wire?.muted == true }
        do { try await session.submitPCM(Data([0, 0]), ticket: ticket); XCTFail("muted PCM admitted") } catch {}
        try await session.setMuted(false)
        try await eventually { await session.machine.wire?.muted == false }
        try await session.submitPCM(Data([0, 0]), ticket: ticket)
        try await session.endUtterance()
        try await eventually { await session.machine.wire?.inputOpen == false }
        try await session.setMuted(false)
        let open = await session.machine.wire?.inputOpen; XCTAssertEqual(open, false)
        let sequence = await session.machine.wire?.inputSequence; XCTAssertEqual(sequence, 1)
        await session.stop()
    }
    func testInputQueueCountsAdmittedSendAndOverflowClosesWithoutReplay() async throws {
        let socket = FakeVoiceSocket(), audio = FakeAudio(), observer = FakeObserver()
        await socket.configure(holdPCM: true)
        let session = VoiceSession(claim: socket.claim, audio: audio, observer: observer)
        try await session.start(socket: socket)
        try await eventually { await session.state == .listening }
        let ticket = try await session.beginUtterance()
        try await session.submitPCM(Data(repeating: 0, count: 640), ticket: ticket)
        try await eventually { await socket.activeSends == 1 }
        for _ in 0..<9 { try await session.submitPCM(Data(repeating: 0, count: 640), ticket: ticket) }
        do { try await session.submitPCM(Data([0, 0]), ticket: ticket); XCTFail("queue exceeded 6400 including in-flight") } catch {}
        await session.stop()
        let closed = await socket.closed, active = await socket.activeSends
        XCTAssertTrue(closed); XCTAssertEqual(active, 0)
        let state = await session.state; XCTAssertEqual(state, .error)
        let packets = await socket.sent
        XCTAssertEqual(packets.filter { if case .binary = $0 { return true }; return false }.count, 1)
    }
    func testSlowOutputIncludesScheduledPCMInLimitAndCleansUp() async throws {
        let socket = FakeVoiceSocket(), audio = FakeAudio(), observer = FakeObserver()
        audio.holdPlayback = true
        let session = VoiceSession(claim: socket.claim, audio: audio, observer: observer)
        try await session.start(socket: socket)
        try await eventually { await session.state == .listening }
        try await socket.inject("generation", ["generation": 1, "phase": "started"])
        for sequence in 0..<11 {
            await socket.push(.binary(try AudioEnvelope(kind: .output, connection: 1, generation: 1,
                sequence: UInt32(sequence), pcm: Data(repeating: 0, count: 960)).encode()))
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        try await eventually { await socket.closed }; await session.stop()
        XCTAssertTrue(audio.scheduled.isEmpty)
        let state = await session.state; XCTAssertEqual(state, .error)
    }
}

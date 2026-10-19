import XCTest
@testable import CampusPulseVoice

@MainActor final class TestPlaybackScheduler: PlaybackScheduling {
    struct Scheduled {
        let ordinal: Int
        let marker: UInt8
        let bytes: Int
        let completion: @Sendable (PlaybackOutcome) -> Void
    }
    var pending: [Int: Scheduled] = [:]
    var history: [Scheduled] = []
    var clears = 0
    var shutdowns = 0
    var failAfterOrdinal: Int?
    var completeDuringClear = false
    var beforeClear: (@MainActor () -> Void)?
    var maximumBytes = 0
    var maximumPackets = 0
    var callbacks: [@Sendable (PlaybackOutcome) -> Void] { pending.keys.sorted().compactMap { pending[$0]?.completion } }
    var scheduledBytes: Int { pending.values.reduce(0) { $0 + $1.bytes } }
    func schedule(_ pcm: Data, completion: @escaping @Sendable (PlaybackOutcome) -> Void) throws {
        let index = history.count
        let entry = Scheduled(ordinal: index, marker: pcm.first ?? 0, bytes: pcm.count, completion: completion)
        history.append(entry); pending[index] = entry
        maximumBytes = max(maximumBytes, scheduledBytes); maximumPackets = max(maximumPackets, pending.count)
        if failAfterOrdinal == index { throw AudioFailure.graph }
    }
    func complete(_ ordinal: Int, outcome: PlaybackOutcome = .consumed) {
        guard let entry = pending[ordinal] else { return }
        if outcome != .failed { pending.removeValue(forKey: ordinal) }
        entry.completion(outcome)
    }
    func clear() {
        clears += 1; beforeClear?()
        let removed = Array(pending.values); pending.removeAll()
        if completeDuringClear { removed.forEach { $0.completion(.consumed) } }
    }
    func shutdown() { shutdowns += 1; clear() }
}

enum NativeOutputTest {
    static func frame(sequence: UInt32 = 0, bytes: Int = 960, generation: UInt32 = 1) -> AudioEnvelope {
        var pcm = Data(repeating: 0, count: bytes); pcm[0] = UInt8(truncatingIfNeeded: sequence)
        return AudioEnvelope(kind: .output, connection: 1, generation: generation, sequence: sequence, pcm: pcm)
    }
    static func admission(_ frame: AudioEnvelope, epoch: UUID, serial: UInt64 = 0, id: UUID = UUID()) -> PlaybackAdmission {
        PlaybackAdmission(id: id, ticket: PlaybackTicket(epoch: epoch, serial: serial, generation: frame.generation),
                          connection: frame.connection, sequence: frame.sequence, pcmBytes: frame.pcm.count)
    }
}

@MainActor final class AudioPlayerTests: XCTestCase {
    func testTwoSynchronousAdmissionsBeforeAnyPlayedBackCompletion() throws {
        let native = TestPlaybackScheduler(), player = AudioPlayer(scheduler: native), epoch = UUID()
        let receipts = try (0..<2).map { index in
            let frame = NativeOutputTest.frame(sequence: UInt32(index))
            return try player.scheduleOutput(frame, admission: NativeOutputTest.admission(frame, epoch: epoch))
        }
        XCTAssertEqual(native.history.map(\.marker), [0, 1])
        XCTAssertTrue(receipts.allSatisfy { $0.consumption.resolvedOutcome == nil })
        XCTAssertEqual(player.retainedBytes, 1920); XCTAssertEqual(player.retainedPackets, 2)
        native.complete(0)
        XCTAssertEqual(player.retainedBytes, 960)
        XCTAssertEqual(receipts[0].consumption.resolvedOutcome, .consumed)
        player.shutdown(); XCTAssertEqual(receipts[1].consumption.resolvedOutcome, .cancelled)
    }
    func testWaiterCancellationNeverClaimsNativeConsumptionOrReleasesCharge() async throws {
        let native = TestPlaybackScheduler(), player = AudioPlayer(scheduler: native), frame = NativeOutputTest.frame()
        let receipt = try player.scheduleOutput(frame, admission: NativeOutputTest.admission(frame, epoch: UUID()))
        let waiting = Task { try await receipt.consumption.wait() }; waiting.cancel()
        do { try await waiting.value; XCTFail("cancelled waiter succeeded") } catch {}
        XCTAssertNil(receipt.consumption.resolvedOutcome)
        XCTAssertEqual(player.retainedBytes, 960); XCTAssertEqual(native.pending.count, 1); XCTAssertEqual(native.clears, 0)
        player.flush(epoch: receipt.admission.ticket.epoch, nextSerial: 1)
        XCTAssertEqual(receipt.consumption.resolvedOutcome, .cancelled); XCTAssertEqual(player.retainedBytes, 0)
        player.shutdown()
    }
    func testFlushPhysicallyClearsBeforeResolvingAndSuppressesCallbacksDuringClear() throws {
        let native = TestPlaybackScheduler(), player = AudioPlayer(scheduler: native), frame = NativeOutputTest.frame()
        let receipt = try player.scheduleOutput(frame, admission: NativeOutputTest.admission(frame, epoch: UUID()))
        let late = native.history[0].completion
        native.completeDuringClear = true
        native.beforeClear = {
            XCTAssertEqual(player.retainedBytes, 960)
            XCTAssertNil(receipt.consumption.resolvedOutcome)
        }
        player.flush(epoch: receipt.admission.ticket.epoch, nextSerial: 1)
        native.beforeClear = nil
        XCTAssertTrue(native.pending.isEmpty); XCTAssertEqual(player.retainedBytes, 0)
        XCTAssertEqual(receipt.consumption.resolvedOutcome, .cancelled)
        late(.consumed); late(.failed)
        XCTAssertEqual(receipt.consumption.resolvedOutcome, .cancelled)
        XCTAssertThrowsError(try player.scheduleOutput(frame, admission: receipt.admission))
        player.shutdown()
    }
    func testByteLimitIncludesScheduledNativeBuffers() throws {
        let native = TestPlaybackScheduler(), player = AudioPlayer(scheduler: native), epoch = UUID()
        for index in 0..<5 {
            let frame = NativeOutputTest.frame(sequence: UInt32(index), bytes: 1920)
            _ = try player.scheduleOutput(frame, admission: NativeOutputTest.admission(frame, epoch: epoch))
        }
        let extra = NativeOutputTest.frame(sequence: 5, bytes: 2)
        XCTAssertThrowsError(try player.scheduleOutput(extra, admission: NativeOutputTest.admission(extra, epoch: epoch)))
        XCTAssertEqual(player.retainedBytes, 9600); XCTAssertEqual(native.history.count, 5)
        player.shutdown(); XCTAssertEqual(player.retainedBytes, 0)
    }
    func testPacketLimitIncludesTinyPendingReceipts() throws {
        let native = TestPlaybackScheduler(), player = AudioPlayer(scheduler: native), epoch = UUID()
        for index in 0..<32 {
            let frame = NativeOutputTest.frame(sequence: UInt32(index), bytes: 2)
            _ = try player.scheduleOutput(frame, admission: NativeOutputTest.admission(frame, epoch: epoch))
        }
        let extra = NativeOutputTest.frame(sequence: 32, bytes: 2)
        XCTAssertThrowsError(try player.scheduleOutput(extra, admission: NativeOutputTest.admission(extra, epoch: epoch)))
        XCTAssertEqual(player.retainedPackets, 32); XCTAssertEqual(player.retainedBytes, 64)
        player.shutdown()
    }
    func testExactAdmissionFieldsDuplicateIDAndSequenceAreValidated() throws {
        let native = TestPlaybackScheduler(), player = AudioPlayer(scheduler: native), epoch = UUID(), frame = NativeOutputTest.frame()
        let valid = NativeOutputTest.admission(frame, epoch: epoch)
        let wrong = [
            PlaybackAdmission(id: UUID(), ticket: valid.ticket, connection: 2, sequence: 0, pcmBytes: 960),
            PlaybackAdmission(id: UUID(), ticket: valid.ticket, connection: 1, sequence: 1, pcmBytes: 960),
            PlaybackAdmission(id: UUID(), ticket: valid.ticket, connection: 1, sequence: 0, pcmBytes: 2),
            PlaybackAdmission(id: UUID(), ticket: PlaybackTicket(epoch: epoch, serial: 0, generation: 2), connection: 1, sequence: 0, pcmBytes: 960),
        ]
        for admission in wrong { XCTAssertThrowsError(try player.scheduleOutput(frame, admission: admission)) }
        XCTAssertTrue(native.history.isEmpty)
        _ = try player.scheduleOutput(frame, admission: valid)
        let second = NativeOutputTest.frame(sequence: 1)
        XCTAssertThrowsError(try player.scheduleOutput(second, admission: NativeOutputTest.admission(second, epoch: epoch, id: valid.id)))
        let skipped = NativeOutputTest.frame(sequence: 3)
        XCTAssertThrowsError(try player.scheduleOutput(skipped, admission: NativeOutputTest.admission(skipped, epoch: epoch)))
        player.shutdown()
    }
    func testThrowAfterEnqueueRetainsUnknownChargeThroughActualClear() throws {
        let native = TestPlaybackScheduler(), player = AudioPlayer(scheduler: native), epoch = UUID(), first = NativeOutputTest.frame()
        let receipt = try player.scheduleOutput(first, admission: NativeOutputTest.admission(first, epoch: epoch))
        native.failAfterOrdinal = 1
        native.beforeClear = { XCTAssertEqual(player.retainedBytes, 1920); XCTAssertNil(receipt.consumption.resolvedOutcome) }
        let second = NativeOutputTest.frame(sequence: 1)
        XCTAssertThrowsError(try player.scheduleOutput(second, admission: NativeOutputTest.admission(second, epoch: epoch)))
        native.beforeClear = nil
        XCTAssertTrue(native.pending.isEmpty); XCTAssertEqual(player.retainedBytes, 0)
        XCTAssertEqual(receipt.consumption.resolvedOutcome, .cancelled)
        player.shutdown()
    }
    func testConsumptionFailureIsNotSuccessAndRetainsUnknownNativeOwnershipUntilFlush() throws {
        let native = TestPlaybackScheduler(), events = AudioEventMailbox(), epoch = UUID()
        let player = AudioPlayer(scheduler: native, onFailure: { events.offer(.playbackFailure) })
        let frame = NativeOutputTest.frame()
        let receipt = try player.scheduleOutput(frame, admission: NativeOutputTest.admission(frame, epoch: epoch))
        native.complete(0, outcome: .failed)
        XCTAssertEqual(receipt.consumption.resolvedOutcome, .failed)
        XCTAssertEqual(player.retainedBytes, 960); XCTAssertEqual(events.take(), .playbackFailure)
        player.flush(epoch: epoch, nextSerial: 1)
        XCTAssertEqual(player.retainedBytes, 0); XCTAssertEqual(receipt.consumption.resolvedOutcome, .failed)
        player.shutdown()
    }
    func testLateLowerFlushCannotClearNewSerialOrReopenOldGeneration() throws {
        let native = TestPlaybackScheduler(), player = AudioPlayer(scheduler: native), epoch = UUID(), first = NativeOutputTest.frame()
        let old = try player.scheduleOutput(first, admission: NativeOutputTest.admission(first, epoch: epoch))
        let late = native.history[0].completion
        player.flush(epoch: epoch, nextSerial: 1)
        let retiredGeneration = NativeOutputTest.frame(sequence: 1)
        XCTAssertThrowsError(try player.scheduleOutput(retiredGeneration,
            admission: NativeOutputTest.admission(retiredGeneration, epoch: epoch, serial: 1)))
        let next = NativeOutputTest.frame(generation: 2)
        let fresh = try player.scheduleOutput(next, admission: NativeOutputTest.admission(next, epoch: epoch, serial: 1))
        player.flush(epoch: epoch, nextSerial: 0); late(.consumed)
        XCTAssertNil(fresh.consumption.resolvedOutcome); XCTAssertEqual(old.consumption.resolvedOutcome, .cancelled)
        XCTAssertEqual(player.retainedBytes, 960)
        XCTAssertThrowsError(try player.scheduleOutput(first, admission: NativeOutputTest.admission(first, epoch: epoch, serial: 1)))
        player.shutdown(); XCTAssertEqual(fresh.consumption.resolvedOutcome, .cancelled)
    }
    func testShutdownResolvesAllReceiptsBeforeReturnAndNeverReopens() throws {
        let native = TestPlaybackScheduler(), player = AudioPlayer(scheduler: native), frame = NativeOutputTest.frame()
        let receipt = try player.scheduleOutput(frame, admission: NativeOutputTest.admission(frame, epoch: UUID()))
        let late = native.history[0].completion
        player.shutdown(); late(.consumed)
        XCTAssertEqual(receipt.consumption.resolvedOutcome, .cancelled)
        XCTAssertEqual(player.retainedBytes, 0); XCTAssertEqual(native.shutdowns, 1)
        XCTAssertThrowsError(try player.scheduleOutput(frame, admission: receipt.admission))
    }
}

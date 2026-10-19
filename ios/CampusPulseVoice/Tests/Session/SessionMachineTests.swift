import XCTest
@testable import CampusPulseVoice

enum TestEvent {
    static func data(_ type: String, sequence: UInt64, connection: UInt64 = 1, elapsed: UInt64? = nil,
                     _ extra: [String: Any] = [:]) throws -> Data {
        var body: [String: Any] = ["version": "1", "type": type, "event_id": "event_\(sequence)",
            "event_sequence": sequence, "connection_generation": connection, "elapsed_ms": elapsed ?? sequence]
        extra.forEach { body[$0.key] = $0.value }
        return try JSONSerialization.data(withJSONObject: body)
    }
    static func ready(connection: UInt64 = 1) throws -> Data {
        try data("ready", sequence: 0, connection: connection, ["run_id": "run_test", "session_id": "session_test",
            "expires_in_ms": 540000, "input_format": WireJSON.inputFormat, "output_format": WireJSON.outputFormat])
    }
    static func active() throws -> SessionMachine {
        var m = SessionMachine(); _ = try m.begin(now: 0); _ = try m.receive(.text(ready()), now: 0.01); return m
    }
    static func transcript(sequence: UInt64, revision: UInt64 = 0, final: Bool = true,
                           speaker: String = "user", text: String = "закончить разговор", generation: UInt64 = 0) throws -> Data {
        try data("transcript", sequence: sequence, ["transcript_id": "transcript_test", "revision": revision,
            "final": final, "speaker": speaker, "generation": generation, "text": text])
    }
}

final class SessionMachineTests: XCTestCase {
    private func outputFrame(_ sequence: UInt32, bytes: Int = 960, generation: UInt32 = 1) -> AudioEnvelope {
        AudioEnvelope(kind: .output, connection: 1, generation: generation, sequence: sequence, pcm: Data(repeating: 0, count: bytes))
    }
    private func schedule(_ work: PlaybackLedger.Work) -> PlaybackReceipt {
        PlaybackReceipt(admission: work.admission, consumption: PlaybackConsumption())
    }
    func testPlaybackLedgerChargesQueuedAdmittingAndScheduledBytesUntilConsumption() throws {
        var ledger = PlaybackLedger(epoch: UUID())
        for sequence in 0..<10 { try ledger.enqueue(outputFrame(UInt32(sequence))) }
        let first = ledger.takeNext()!, second = ledger.takeNext()!
        XCTAssertTrue(try ledger.scheduled(schedule(first), expected: first.admission))
        XCTAssertTrue(try ledger.scheduled(schedule(second), expected: second.admission))
        XCTAssertEqual(ledger.bytes, 9600); XCTAssertEqual(ledger.packets, 10)
        XCTAssertThrowsError(try ledger.enqueue(outputFrame(10, bytes: 2)))
        try ledger.finished(second.admission, outcome: .consumed)
        XCTAssertEqual(ledger.bytes, 8640); XCTAssertEqual(ledger.packets, 9)
        try ledger.enqueue(outputFrame(10))
        XCTAssertEqual(ledger.bytes, 9600) // buffer0 still pending and charged
    }
    func testPlaybackPacketCapIncludesAllPendingConsumptionJobs() throws {
        var ledger = PlaybackLedger(epoch: UUID())
        var admissions: [PlaybackAdmission] = []
        for sequence in 0..<32 {
            try ledger.enqueue(outputFrame(UInt32(sequence), bytes: 2))
            let work = ledger.takeNext()!
            XCTAssertTrue(try ledger.scheduled(schedule(work), expected: work.admission)); admissions.append(work.admission)
        }
        XCTAssertEqual(ledger.bytes, 64); XCTAssertEqual(ledger.packets, 32)
        XCTAssertThrowsError(try ledger.enqueue(outputFrame(32, bytes: 2)))
        let serial = ledger.beginFlush(); ledger.confirmFlush(nextSerial: serial)
        XCTAssertEqual(ledger.packets, 32) // actual flush alone doesn't retire running children
        for admission in admissions { try ledger.finished(admission, outcome: .cancelled) }
        XCTAssertEqual(ledger.bytes, 0); XCTAssertEqual(ledger.packets, 0)
    }
    func testPlaybackFlushAcknowledgementAndWaiterRetirementAreBothRequired() throws {
        var ledger = PlaybackLedger(epoch: UUID())
        try ledger.enqueue(outputFrame(0)); try ledger.enqueue(outputFrame(1))
        let admitting = ledger.takeNext()!
        let serial = ledger.beginFlush()
        XCTAssertEqual(ledger.bytes, 960) // only never-scheduled queued data discarded
        XCTAssertFalse(try ledger.schedulingFailed(admitting.admission))
        XCTAssertEqual(ledger.bytes, 960) // unknown native scheduling outcome still charged
        ledger.confirmFlush(nextSerial: serial)
        XCTAssertEqual(ledger.bytes, 0)
        try ledger.enqueue(outputFrame(0, generation: 2)); let old = ledger.takeNext()!
        XCTAssertTrue(try ledger.scheduled(schedule(old), expected: old.admission))
        let next = ledger.beginFlush()
        try ledger.finished(old.admission, outcome: .cancelled)
        XCTAssertEqual(ledger.bytes, 960) // waiter ended, native cancellation not yet acknowledged
        ledger.confirmFlush(nextSerial: next)
        XCTAssertEqual(ledger.bytes, 0)
    }
    func testPlaybackFailureNeverReleasesUnknownNativePCMBeforeFlush() throws {
        var ledger = PlaybackLedger(epoch: UUID())
        try ledger.enqueue(outputFrame(0)); let work = ledger.takeNext()!
        XCTAssertTrue(try ledger.scheduled(schedule(work), expected: work.admission))
        XCTAssertThrowsError(try ledger.finished(work.admission, outcome: .failed))
        XCTAssertEqual(ledger.bytes, 960)
        let serial = ledger.beginFlush(); ledger.confirmFlush(nextSerial: serial)
        XCTAssertEqual(ledger.bytes, 0)
    }
    func testLateAdmissionAcknowledgmentAfterFlushCannotStartAWaiterOrRetireNewSerial() throws {
        var ledger = PlaybackLedger(epoch: UUID())
        try ledger.enqueue(outputFrame(0)); let old = ledger.takeNext()!
        let receipt = schedule(old)
        let serial = ledger.beginFlush()
        receipt.consumption.resolve(.cancelled) // fake native clear completed
        ledger.confirmFlush(nextSerial: serial)
        XCTAssertEqual(ledger.bytes, 960) // MainActor admission has not returned
        try ledger.enqueue(outputFrame(0, generation: 2))
        XCTAssertFalse(try ledger.scheduled(receipt, expected: old.admission))
        XCTAssertEqual(ledger.bytes, 960); XCTAssertEqual(ledger.packets, 1)
        receipt.consumption.resolve(.consumed)
        XCTAssertEqual(receipt.consumption.resolvedOutcome, .cancelled)
    }
    func testPlaybackWrongReceiptCannotRetireOrRelabelAnAdmission() throws {
        var ledger = PlaybackLedger(epoch: UUID())
        try ledger.enqueue(outputFrame(0)); let work = ledger.takeNext()!
        let wrong = PlaybackAdmission(id: UUID(), ticket: work.admission.ticket, connection: 1, sequence: 0, pcmBytes: 960)
        XCTAssertThrowsError(try ledger.scheduled(PlaybackReceipt(admission: wrong, consumption: PlaybackConsumption()), expected: work.admission))
        XCTAssertEqual(ledger.bytes, 960)
        XCTAssertTrue(try ledger.schedulingFailed(work.admission))
        let serial = ledger.beginFlush(); ledger.confirmFlush(nextSerial: serial)
        XCTAssertEqual(ledger.bytes, 0)
    }
    func testPlaybackLateOldCompletionCannotFreeNewSerialOrDoubleRetire() throws {
        var ledger = PlaybackLedger(epoch: UUID())
        try ledger.enqueue(outputFrame(0)); let old = ledger.takeNext()!
        XCTAssertTrue(try ledger.scheduled(schedule(old), expected: old.admission))
        let serial = ledger.beginFlush(); ledger.confirmFlush(nextSerial: serial)
        try ledger.enqueue(outputFrame(0, generation: 2))
        XCTAssertEqual(ledger.bytes, 1920)
        try ledger.finished(old.admission, outcome: .cancelled)
        XCTAssertEqual(ledger.bytes, 960)
        XCTAssertThrowsError(try ledger.finished(old.admission, outcome: .consumed))
        XCTAssertEqual(ledger.bytes, 960)
    }
    func testPlaybackWaitCancellationDoesNotClaimPhysicalCompletionAndLateCallbackIsLatched() async throws {
        let consumption = PlaybackConsumption()
        let waiter = Task { try await consumption.wait() }
        waiter.cancel()
        do { try await waiter.value; XCTFail("cancelled waiter succeeded") } catch {}
        XCTAssertNil(consumption.resolvedOutcome)
        consumption.resolve(.cancelled) // only after fake native clear acknowledgment
        consumption.resolve(.consumed) // stale native callback
        XCTAssertEqual(consumption.resolvedOutcome, .cancelled)
    }
    func testStopBeforeReadyRequiresJoinedCleanupBeforeRestart() throws {
        var m = SessionMachine(); _ = try m.begin(now: 0)
        XCTAssertNil(try m.stop(now: 0.1, request: "stop"))
        XCTAssertEqual(m.state, .stopping); XCTAssertTrue(m.terminal)
        XCTAssertThrowsError(try m.begin(now: 0.2))
        m.cleanupCompleted(failed: false)
        XCTAssertEqual(m.state, .idle)
        _ = try m.begin(now: 0.3)
        _ = try m.receive(.text(TestEvent.ready()), now: 0.4)
        XCTAssertEqual(m.state, .listening)
    }
    func testSameClaimKeepsGenerationFenceAfterCleanupNewMachineMayStartOne() throws {
        var same = try TestEvent.active(); same.cleanupCompleted(failed: false)
        _ = try same.begin(now: 1)
        XCTAssertThrowsError(try same.receive(.text(TestEvent.ready()), now: 1.01))
        _ = try same.receive(.text(TestEvent.ready(connection: 2)), now: 1.02)
        var newClaim = SessionMachine(); _ = try newClaim.begin(now: 1)
        _ = try newClaim.receive(.text(TestEvent.ready()), now: 1.01)
        XCTAssertEqual(newClaim.state, .listening)
    }
    func testReadyInputOpenFirstGenerationOneAndDuplexDuringResponse() throws {
        var m = try TestEvent.active()
        XCTAssertEqual(m.wire?.inputOpen, true)
        _ = try m.receive(.text(TestEvent.data("generation", sequence: 1, ["generation": 1, "phase": "started"])), now: 0.02)
        XCTAssertEqual(m.state, .responding)
        let frame = try m.admitPCM(Data(repeating: 0, count: 640), now: 0.03)
        XCTAssertEqual(frame.generation, 0); XCTAssertEqual(frame.sequence, 0)
        XCTAssertEqual(m.wire?.eventSequence, 2); XCTAssertEqual(m.wire?.inputSequence, 1)
        XCTAssertThrowsError(try m.receive(.text(TestEvent.data("generation", sequence: 2, ["generation": 2, "phase": "started"])), now: 0.04))
    }
    func testExactFinalUserCommandOnlyAfterRevisionValidation() throws {
        var m = try TestEvent.active()
        _ = try m.receive(.text(TestEvent.transcript(sequence: 1, final: false)), now: 0.02)
        XCTAssertNotEqual(m.state, .stopping)
        let effect = try m.receive(.text(TestEvent.transcript(sequence: 2, revision: 1)), now: 0.03)
        guard case .stopCommand = effect else { return XCTFail("validated final user command required") }
        XCTAssertThrowsError(try m.receive(.text(TestEvent.transcript(sequence: 3, revision: 2)), now: 0.04))
        for text in ["Закончить разговор", "закончить разговор.", " закончить разговор", "прошу закончить разговор"] {
            var fresh = try TestEvent.active()
            let result = try fresh.receive(.text(TestEvent.transcript(sequence: 1, text: text)), now: 0.02)
            if case .stopCommand = result { XCTFail("must require exact text") }
        }
        var assistant = try TestEvent.active()
        _ = try assistant.receive(.text(TestEvent.data("generation", sequence: 1, ["generation": 1, "phase": "started"])), now: 0.02)
        let result = try assistant.receive(.text(TestEvent.transcript(sequence: 2, speaker: "assistant", generation: 1)), now: 0.03)
        if case .stopCommand = result { XCTFail("assistant text has no control authority") }
    }
    func testReplacementRevisionsStartZeroAndFinalIsImmutable() throws {
        var m = try TestEvent.active()
        XCTAssertThrowsError(try m.receive(.text(TestEvent.transcript(sequence: 1, revision: 1, final: false)), now: 0.02))
        _ = try m.receive(.text(TestEvent.transcript(sequence: 1, final: false, text: "старый")), now: 0.02)
        _ = try m.receive(.text(TestEvent.transcript(sequence: 2, revision: 1, final: true, text: "новый")), now: 0.03)
        XCTAssertEqual(m.wire?.transcripts["transcript_test"]?.text, "новый")
        XCTAssertThrowsError(try m.receive(.text(TestEvent.transcript(sequence: 3, revision: 2)), now: 0.04))
    }
    func testStopDrainsLegitimateCrossingAckGenerationAudioAndTextWithoutEffects() throws {
        var m = try TestEvent.active()
        try m.admit(WireJSON.control("heartbeat", request: "sent_ping", connection: 1), now: 0.02)
        XCTAssertNotNil(try m.stop(now: 0.03, request: "stop"))
        XCTAssertNil(try m.stop(now: 0.04, request: "stop_again"))
        XCTAssertThrowsError(try m.admitPCM(Data([0, 0]), now: 0.04))
        let packets: [VoicePacket] = [
            .text(try TestEvent.data("heartbeat_ack", sequence: 1, ["request_id": "sent_ping"])),
            .text(try TestEvent.data("generation", sequence: 2, ["generation": 1, "phase": "started"])),
            .binary(try AudioEnvelope(kind: .output, connection: 1, generation: 1, sequence: 0, pcm: Data([0, 0])).encode()),
            .text(try TestEvent.transcript(sequence: 3))]
        for packet in packets {
            guard case .discard = try m.receive(packet, now: 0.05) else { return XCTFail("no data effects after Stop") }
        }
        XCTAssertEqual(m.wire?.phase, .closing)
        XCTAssertEqual(m.receiveDrain?.eventSequence, 4)
        _ = try m.receive(.text(TestEvent.data("closed", sequence: 4, ["reason": "stopped"])), now: 0.06)
        XCTAssertTrue(m.terminal); XCTAssertEqual(m.state, .idle)
        XCTAssertThrowsError(try m.receive(.text(TestEvent.data("closed", sequence: 5, ["reason": "stopped"])), now: 0.07))
    }
    func testStopRejectsInvalidCrossingEventsAndHasPacketAndTimeBounds() throws {
        let invalid: [VoicePacket] = [
            .text(try TestEvent.data("heartbeat_ack", sequence: 1, ["request_id": "never_sent"])),
            .text(try TestEvent.data("generation", sequence: 2, ["generation": 1, "phase": "started"])),
            .text(try TestEvent.data("generation", sequence: 1, connection: 2, ["generation": 1, "phase": "started"])),
            .text(try TestEvent.transcript(sequence: 1, revision: 1)),
            .text(try TestEvent.data("tool_finished", sequence: 1, ["call_id": "missing", "tool": "read_calendar", "status": "success", "duration_ms": 1])),
            .binary(try AudioEnvelope(kind: .output, connection: 1, generation: 1, sequence: 0, pcm: Data([0, 0])).encode())]
        for packet in invalid {
            var m = try TestEvent.active(); _ = try m.stop(now: 0.02, request: "stop")
            XCTAssertThrowsError(try m.receive(packet, now: 0.03))
        }
        var m = try TestEvent.active(); _ = try m.stop(now: 0.02, request: "stop")
        for sequence in 1...32 {
            _ = try m.receive(.text(TestEvent.data("error", sequence: UInt64(sequence), ["code": "timeout", "fatal": false])), now: 0.03)
        }
        XCTAssertThrowsError(try m.receive(.text(TestEvent.data("closed", sequence: 33, ["reason": "stopped"])), now: 0.04))
        var timed = try TestEvent.active(); _ = try timed.stop(now: 0.02, request: "stop")
        XCTAssertThrowsError(try timed.checkTime(now: 2.02))
        XCTAssertThrowsError(try timed.receive(.text(TestEvent.data("closed", sequence: 1, ["reason": "stopped"])), now: 2.02))
    }
    func testOrderedSentHeartbeatsOnlyAndClosedMayDiscardPendingAck() throws {
        var m = try TestEvent.active()
        try m.admit(WireJSON.control("heartbeat", request: "one", connection: 1), now: 0.02)
        try m.admit(WireJSON.control("heartbeat", request: "two", connection: 1), now: 0.03)
        _ = try m.stop(now: 0.04, request: "stop")
        XCTAssertThrowsError(try m.receive(.text(TestEvent.data("heartbeat_ack", sequence: 1, ["request_id": "two"])), now: 0.05))
        _ = try m.receive(.text(TestEvent.data("heartbeat_ack", sequence: 1, ["request_id": "one"])), now: 0.05)
        XCTAssertThrowsError(try m.receive(.text(TestEvent.data("heartbeat_ack", sequence: 2, ["request_id": "one"])), now: 0.06))
        _ = try m.receive(.text(TestEvent.data("closed", sequence: 2, ["reason": "stopped"])), now: 0.07)
        XCTAssertTrue(m.terminal)
    }
    func testInterruptionFencesOldOutputAndReconnectHasNoAudioReplay() throws {
        var m = try TestEvent.active()
        _ = try m.receive(.text(TestEvent.data("generation", sequence: 1, ["generation": 1, "phase": "started"])), now: 0.02)
        _ = try m.receive(.text(TestEvent.data("interrupted", sequence: 2, ["generation": 1])), now: 0.03)
        XCTAssertEqual(m.state, .interrupted)
        let stale = try AudioEnvelope(kind: .output, connection: 1, generation: 1, sequence: 0, pcm: Data([0, 0])).encode()
        XCTAssertThrowsError(try m.receive(.binary(stale), now: 0.04))
        // Input is still independent of interrupted UI/output state.
        _ = try m.admitPCM(Data([0, 0]), now: 0.04)
        m.resetForReconnect(); _ = try m.begin(now: 1, reconnect: true)
        XCTAssertNil(m.wire)
        XCTAssertThrowsError(try m.receive(.text(TestEvent.ready()), now: 1.01))
        _ = try m.receive(.text(TestEvent.ready(connection: 2)), now: 1.01)
        XCTAssertEqual(m.wire?.inputSequence, 0); XCTAssertEqual(m.wire?.generation, 0)
        XCTAssertTrue(m.wire?.transcripts.isEmpty == true)
    }
    func testInputEndMuteRateBudgetsAndNoCounterWrap() throws {
        var m = try TestEvent.active()
        try m.admit(WireJSON.control("input", request: "end", connection: 1, action: "end"), now: 0.02)
        XCTAssertThrowsError(try m.admitPCM(Data([0, 0]), now: 0.03))
        XCTAssertThrowsError(try m.admit(WireJSON.control("input", request: "again", connection: 1, action: "end"), now: 0.03))
        try m.admit(WireJSON.control("input", request: "begin", connection: 1, action: "begin"), now: 0.04)
        try m.admit(WireJSON.control("mute", request: "mute", connection: 1, muted: true), now: 0.05)
        XCTAssertThrowsError(try m.admitPCM(Data([0, 0]), now: 0.06))
        try m.admit(WireJSON.control("mute", request: "unmute", connection: 1, muted: false), now: 0.07)
        for _ in 0..<2 { _ = try m.admitPCM(Data(repeating: 0, count: 1280), now: 0.08) }
        XCTAssertThrowsError(try m.admitPCM(Data([0, 0]), now: 0.08))
        XCTAssertEqual(m.wire?.inputSequence, 2)
        XCTAssertThrowsError(try WireJSON.control("heartbeat", request: "bad", connection: 0x100000000))
        _ = try AudioEnvelope(kind: .input, connection: .max, generation: 0, sequence: .max, pcm: Data([0, 0])).encode()
    }
    func testReadyIdleAndLivenessTimeouts() throws {
        var pending = SessionMachine(); _ = try pending.begin(now: 0)
        XCTAssertThrowsError(try pending.checkTime(now: 5))
        let active = try TestEvent.active()
        XCTAssertThrowsError(try active.checkTime(now: 30.01))
        XCTAssertThrowsError(try active.checkTime(now: 540))
    }
}

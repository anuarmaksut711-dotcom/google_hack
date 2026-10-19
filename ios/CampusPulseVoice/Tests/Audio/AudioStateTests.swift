import XCTest
@testable import CampusPulseVoice

final class AudioStateTests: XCTestCase {
    func testNominal20msAndFinalPartialAreNotPadded() throws {
        var chunks = PCMChunker()
        XCTAssertEqual(try chunks.append(Data(repeating: 1, count: 638)), [])
        let full = try chunks.append(Data(repeating: 2, count: 644))
        XCTAssertEqual(full.map(\.count), [640, 640])
        XCTAssertEqual(chunks.finish(), Data([2, 2]))
        XCTAssertNil(chunks.finish())
    }
    func testOddPCMDoesNotChangePartial() throws {
        var chunks = PCMChunker()
        _ = try chunks.append(Data([1, 2]))
        XCTAssertThrowsError(try chunks.append(Data([3])))
        XCTAssertEqual(chunks.finish(), Data([1, 2]))
    }
    func testPacerSeparatesBatchedChunksAndDoesNotCatchUpInABurst() throws {
        var pacer = CapturePacer()
        XCTAssertEqual(try pacer.reserve(bytes: 640, now: 10), 0, accuracy: 0.000001)
        XCTAssertEqual(try pacer.reserve(bytes: 640, now: 10), 0.020, accuracy: 0.000001)
        XCTAssertEqual(try pacer.reserve(bytes: 320, now: 10.020), 0.020, accuracy: 0.000001)
        XCTAssertEqual(try pacer.reserve(bytes: 640, now: 11), 0, accuracy: 0.000001)
        XCTAssertEqual(try pacer.reserve(bytes: 640, now: 11), 0.020, accuracy: 0.000001)
        XCTAssertThrowsError(try pacer.reserve(bytes: 640, now: 10))
    }
    func testStoppedCaptureCannotBeStartedAgainEvenInSameEpoch() throws {
        var fences = AudioFences(); try fences.authorize()
        let first = CaptureTicket(epoch: UUID(), connection: 1, utterance: UUID())
        try fences.begin(first); fences.retire(first)
        XCTAssertThrowsError(try fences.begin(first))
        let next = CaptureTicket(epoch: first.epoch, connection: 1, utterance: UUID())
        try fences.begin(next)
        fences.retire(first) // Late stop must not remove the new turn.
        XCTAssertEqual(fences.capture, next)
    }
    func testCloseBeforeAnyDataRetiresEpoch() throws {
        var fences = AudioFences(); let old = UUID()
        XCTAssertFalse(fences.close(old))
        try fences.authorize()
        XCTAssertThrowsError(try fences.begin(CaptureTicket(epoch: old, connection: 1, utterance: UUID())))
        let pending = UUID()
        XCTAssertTrue(fences.close(pending)) // Authorized pre-Ready close stops audio too.
        XCTAssertFalse(fences.authorized)
        try fences.authorize()
        XCTAssertThrowsError(try fences.begin(CaptureTicket(epoch: pending, connection: 2, utterance: UUID())))
    }
    func testCloseAndStaleFlushCannotTouchNewEpoch() throws {
        var fences = AudioFences(); try fences.authorize()
        let old = CaptureTicket(epoch: UUID(), connection: 1, utterance: UUID())
        try fences.begin(old); XCTAssertTrue(fences.close(old.epoch)); try fences.authorize()
        let fresh = CaptureTicket(epoch: UUID(), connection: 2, utterance: UUID())
        try fences.begin(fresh)
        XCTAssertFalse(fences.close(old.epoch))
        XCTAssertFalse(fences.flush(epoch: old.epoch, nextSerial: 100))
        XCTAssertEqual(fences.capture, fresh)
    }
    func testFlushFloorAndGenerationNeverRegress() throws {
        var fences = AudioFences(); try fences.authorize(); let epoch = UUID()
        XCTAssertTrue(fences.flush(epoch: epoch, nextSerial: 3))
        XCTAssertTrue(fences.flush(epoch: epoch, nextSerial: 1))
        XCTAssertThrowsError(try fences.accept(PlaybackTicket(epoch: epoch, serial: 2, generation: 1)))
        try fences.accept(PlaybackTicket(epoch: epoch, serial: 3, generation: 2))
        XCTAssertThrowsError(try fences.accept(PlaybackTicket(epoch: epoch, serial: 3, generation: 1)))
    }
    func testRetirementMetadataIsBoundedAndExhaustionFailsClosed() throws {
        var fences = AudioFences(); try fences.authorize(); let epoch = UUID()
        for _ in 0..<1025 { fences.retire(CaptureTicket(epoch: epoch, connection: 1, utterance: UUID())) }
        XCTAssertFalse(fences.authorized)
        XCTAssertThrowsError(try fences.authorize())
    }
    func testEventMailboxCoalescesAndCannotReopenAfterClose() async {
        let events = AudioEventMailbox()
        events.offer(.headphonesRemoved); events.offer(.routeChanged)
        XCTAssertEqual(events.take(), .headphonesRemoved)
        events.close(); events.offer(.mediaReset)
        XCTAssertNil(events.take())
        var iterator = events.wakeup.stream.makeAsyncIterator()
        // There may be one previously buffered wakeup, followed by termination.
        _ = await iterator.next()
        let next = await iterator.next(); XCTAssertNil(next)
    }
}

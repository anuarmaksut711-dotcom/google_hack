import AVFoundation
import XCTest
@testable import CampusPulseVoice

@MainActor final class TestAudioSessionDriver: AudioSessionDriving {
    var canStart = true
    var allowed = true
    var holdPermission = false
    var permission: CheckedContinuation<Bool, Never>?
    var requests = 0
    var activations = 0
    var deactivations = 0
    func requestPermission() async -> Bool {
        requests += 1
        if holdPermission { return await withCheckedContinuation { permission = $0 } }
        return allowed
    }
    func activate() throws { activations += 1 }
    func deactivate() { deactivations += 1 }
}

@MainActor final class TestAudioGraph: AudioGraph {
    let native: TestPlaybackScheduler
    let player: AudioPlayer
    let events: AudioEventMailbox
    var starts = 0
    var armAttempts = 0
    var failArm = false
    var beforeArm: (@MainActor () -> Void)?
    var stops = 0
    var shutdowns = 0
    var ticket: CaptureTicket?
    var revision: UUID?
    var mailbox: CaptureMailbox?
    init(events: AudioEventMailbox) {
        let scheduler = TestPlaybackScheduler()
        native = scheduler; self.events = events
        player = AudioPlayer(scheduler: scheduler, onFailure: { events.offer(.playbackFailure) })
    }
    func startCapture(ticket: CaptureTicket, revision: UUID, mailbox: CaptureMailbox) throws {
        armAttempts += 1; beforeArm?()
        if failArm { throw AudioFailure.graph }
        let input = try SyntheticAudio.buffer(rate: 16000, frames: 1)
        try mailbox.prepare(ticket: ticket, revision: revision, format: input.format)
        starts += 1; self.ticket = ticket; self.revision = revision; self.mailbox = mailbox
    }
    func stopCapture() { stops += 1 }
    func shutdown() { shutdowns += 1; stopCapture(); player.shutdown() }
    func feed(frames: Int, ticket: CaptureTicket? = nil, revision: UUID? = nil) throws {
        let input = try SyntheticAudio.buffer(rate: 16000, frames: frames)
        mailbox?.ingest(input, ticket: try XCTUnwrap(ticket ?? self.ticket), revision: try XCTUnwrap(revision ?? self.revision))
    }
}

@MainActor final class AudioControllerTests: XCTestCase {
    private func waitFor(_ condition: @MainActor () async -> Bool) async throws {
        for _ in 0..<300 {
            if await condition() { return }
            try await Task.sleep(nanoseconds: 2_000_000)
        }
        throw AudioFailure.drainTimeout
    }
    private func prepare(driver: TestAudioSessionDriver) throws -> (AudioController, VoiceSession, FakeVoiceSocket, TestAudioGraph) {
        let graph = TestAudioGraph(events: AudioEventMailbox())
        // Notification tests below use their own factory with the real mailbox.
        let audio = AudioController(driver: driver) { _ in graph }
        let socket = FakeVoiceSocket(), voice = VoiceSession(claim: socket.claim, audio: audio, observer: FakeObserver())
        try audio.bind(voice)
        return (audio, voice, socket, graph)
    }
    func testPermissionDenialAndBackgroundStartNeverActivateOrBuildGraph() async throws {
        let driver = TestAudioSessionDriver(); driver.allowed = false
        let (audio, voice, _, graph) = try prepare(driver: driver)
        do { try await audio.userStart(); XCTFail("denied start") } catch {}
        XCTAssertEqual(driver.requests, 1); XCTAssertEqual(driver.activations, 0); XCTAssertEqual(graph.starts, 0)
        driver.canStart = false
        do { try await audio.userStart(); XCTFail("background start") } catch {}
        XCTAssertEqual(driver.requests, 1)
        await audio.userStop()
        await voice.stop()
    }
    func testStopWhilePermissionIsPendingFencesLateGrant() async throws {
        let driver = TestAudioSessionDriver(); driver.holdPermission = true
        let (audio, voice, _, _) = try prepare(driver: driver)
        let start = Task { try await audio.userStart() }
        try await waitFor { driver.permission != nil }
        await audio.userStop()
        driver.permission?.resume(returning: true); driver.permission = nil
        do { try await start.value; XCTFail("late grant activated audio") } catch {}
        XCTAssertEqual(driver.activations, 0)
        await voice.stop()
    }
    func testExplicitEndDrainsShortPCMBeforeWireEndAndRetiresCapture() async throws {
        let driver = TestAudioSessionDriver()
        let (audio, voice, socket, graph) = try prepare(driver: driver)
        try await audio.userStart(); try await voice.start(socket: socket)
        try await waitFor { await voice.state == .listening }
        let ready = try await audio.userBeginUtterance(), ticket = ready.ticket
        XCTAssertTrue(audio.isCurrent(ready))
        try await waitFor { graph.starts == 1 }
        try graph.feed(frames: 137)
        try await audio.userEndUtterance()
        try await waitFor { await voice.machine.wire?.inputOpen == false }
        let packets = await socket.sent
        let pcmIndex = try XCTUnwrap(packets.firstIndex { if case .binary = $0 { return true }; return false })
        guard case .binary(let data) = packets[pcmIndex] else { return XCTFail("missing PCM") }
        XCTAssertEqual(try AudioEnvelope.decode(data, expected: .input).pcm.count, 274)
        let endIndex = try XCTUnwrap(packets.firstIndex {
            guard case .text(let data) = $0, let m = try? WireJSON.parse(data, client: true) else { return false }
            return m.type == "input" && (try? m.text("action")) == "end"
        })
        XCTAssertLessThan(pcmIndex, endIndex)
        XCTAssertThrowsError(try audio.startCapture(ticket: ticket))
        let next = try await audio.userBeginUtterance(); XCTAssertNotEqual(next.ticket, ticket)
        try await waitFor { graph.starts == 2 }
        await audio.userStop()
    }
    func testMuteDiscardsPartialAndLateTapCannotEnterUnmutedRevision() async throws {
        let driver = TestAudioSessionDriver()
        let (audio, voice, socket, graph) = try prepare(driver: driver)
        try await audio.userStart(); try await voice.start(socket: socket)
        try await waitFor { await voice.state == .listening }
        let ready = try await audio.userBeginUtterance(), ticket = ready.ticket
        try await waitFor { graph.starts == 1 }
        let oldRevision = try XCTUnwrap(graph.revision)
        try graph.feed(frames: 137)
        try await audio.userMute(true)
        try await waitFor { await voice.machine.wire?.muted == true }
        try graph.feed(frames: 320, ticket: ticket, revision: oldRevision)
        try await audio.userMute(false)
        try await waitFor { graph.starts == 2 }
        XCTAssertNotEqual(graph.revision, oldRevision)
        try graph.feed(frames: 320, ticket: ticket, revision: oldRevision)
        try graph.feed(frames: 320)
        try await waitFor { await voice.machine.wire?.inputSequence == 1 }
        let packets = await socket.sent
        let frames = try packets.compactMap { packet -> AudioEnvelope? in
            if case .binary(let data) = packet { return try AudioEnvelope.decode(data, expected: .input) }; return nil
        }
        XCTAssertEqual(frames.map { $0.pcm.count }, [640])
        await audio.userStop()
    }
    func testHeadphoneLossFlushesAndClosesWithoutAutomaticRestart() async throws {
        let driver = TestAudioSessionDriver()
        var made: [TestAudioGraph] = []
        let audio = AudioController(driver: driver) { events in
            let graph = TestAudioGraph(events: events); made.append(graph); return graph
        }
        let socket = FakeVoiceSocket(), voice = VoiceSession(claim: socket.claim, audio: audio, observer: FakeObserver())
        try audio.bind(voice); try await audio.userStart(); try await voice.start(socket: socket)
        try await waitFor { await voice.state == .listening }
        let ready = try await audio.userBeginUtterance(), ticket = ready.ticket
        try await waitFor { made[0].starts == 1 }
        let frame = NativeOutputTest.frame(bytes: 2)
        let receipt = try audio.scheduleOutput(frame, admission: NativeOutputTest.admission(frame, epoch: ticket.epoch))
        try await waitFor { made[0].native.callbacks.count == 1 }
        let late = made[0].native.callbacks[0]
        made[0].events.offer(.headphonesRemoved)
        try await waitFor { await socket.closed }
        late(.consumed)
        XCTAssertEqual(receipt.consumption.resolvedOutcome, .cancelled)
        XCTAssertEqual(audio.pauseReason, .headphonesRemoved); XCTAssertEqual(made[0].shutdowns, 1)
        made[0].events.offer(.routeChanged); made[0].events.offer(.mediaReset)
        XCTAssertEqual(made.count, 1); XCTAssertEqual(driver.activations, 1)
        XCTAssertThrowsError(try audio.startCapture(ticket: ticket))
        await audio.userStop()
        try await audio.userStart()
        let fresh = FakeVoiceSocket(claim: socket.claim, connection: 2)
        try await voice.start(socket: fresh, reconnect: true)
        try await waitFor { await voice.state == .listening }
        let next = try await audio.userBeginUtterance()
        try await waitFor { made[1].starts == 1 }
        audio.close(epoch: ticket.epoch) // Late close cannot shut down fresh graph.
        XCTAssertEqual(made[1].shutdowns, 0); XCTAssertNotEqual(next.ticket.epoch, ticket.epoch)
        await audio.userStop()
    }
    func testInterruptionResetAndFormatEventsEachRequireExplicitRestart() async throws {
        for reason in [AudioPauseReason.interruption, .mediaReset, .mediaLost, .routeChanged, .engineChanged] {
            let driver = TestAudioSessionDriver()
            var graph: TestAudioGraph?
            let audio = AudioController(driver: driver) { events in
                let made = TestAudioGraph(events: events); graph = made; return made
            }
            let socket = FakeVoiceSocket(), voice = VoiceSession(claim: socket.claim, audio: audio, observer: FakeObserver())
            try audio.bind(voice); try await audio.userStart(); try await voice.start(socket: socket)
            try await waitFor { await voice.state == .listening }
            graph?.events.offer(reason)
            try await waitFor { await socket.closed }
            XCTAssertEqual(audio.pauseReason, reason); XCTAssertEqual(driver.activations, 1)
            await audio.userStop()
        }
    }
}

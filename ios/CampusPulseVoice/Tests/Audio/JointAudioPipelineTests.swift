import XCTest
@testable import CampusPulseVoice



@MainActor final class JointAudioPipelineTests: XCTestCase {
    private func waitFor(_ condition: @MainActor () async -> Bool) async throws {
        for _ in 0..<400 {
            if await condition() { return }
            try await Task.sleep(nanoseconds: 2_000_000)
        }
        throw AudioFailure.drainTimeout
    }
    private func start() async throws -> (AudioController, VoiceSession, FakeVoiceSocket, TestAudioGraph) {
        var made: TestAudioGraph?
        let audio = AudioController(driver: TestAudioSessionDriver()) { events in
            let graph = TestAudioGraph(events: events); made = graph; return graph
        }
        let socket = FakeVoiceSocket(), voice = VoiceSession(claim: socket.claim, audio: audio, observer: FakeObserver())
        try audio.bind(voice); try await audio.userStart(); try await voice.start(socket: socket)
        try await waitFor { await voice.state == .listening }
        try await socket.inject("generation", ["generation": 1, "phase": "started"])
        return (audio, voice, socket, try XCTUnwrap(made))
    }
    private func push(_ socket: FakeVoiceSocket, sequence: UInt32, bytes: Int = 960, generation: UInt32 = 1) async throws {
        await socket.push(.binary(try NativeOutputTest.frame(sequence: sequence, bytes: bytes, generation: generation).encode()))
    }
    private func assertJoined(_ audio: AudioController, _ voice: VoiceSession,
                              _ socket: FakeVoiceSocket, _ graph: TestAudioGraph) async {
        await audio.userStop()
        let usage = await voice.playbackUsage, active = await socket.activeSends, receive = await socket.pendingReceive
        XCTAssertEqual(usage.bytes, 0); XCTAssertEqual(usage.packets, 0)
        XCTAssertEqual(graph.player.retainedBytes, 0); XCTAssertTrue(graph.native.pending.isEmpty)
        XCTAssertEqual(active, 0); XCTAssertFalse(receive)
    }
    func testRealPipelineEnqueuesThreeInOrderBeforeFirstConsumptionThenStopJoins() async throws {
        let (audio, voice, socket, graph) = try await start()
        for sequence in 0..<3 { try await push(socket, sequence: UInt32(sequence)) }
        try await waitFor { graph.native.history.count == 3 }
        XCTAssertEqual(graph.native.history.map(\.marker), [0, 1, 2])
        XCTAssertEqual(graph.player.retainedBytes, 2880); XCTAssertEqual(graph.native.pending.count, 3)
        let held = await voice.playbackUsage
        XCTAssertEqual(held.bytes, 2880); XCTAssertEqual(held.packets, 3)

        graph.native.complete(0)
        try await waitFor { await voice.playbackUsage.bytes == 1920 }
        XCTAssertEqual(graph.player.retainedBytes, 1920)
        let late = graph.native.history.map(\.completion)
        await assertJoined(audio, voice, socket, graph)
        late.forEach { $0(.consumed) }
        XCTAssertEqual(graph.player.retainedBytes, 0)
        let usage = await voice.playbackUsage; XCTAssertEqual(usage.bytes, 0)
    }
    func testRealPipelineByteCapIncludesScheduledBuffersUntilFlushAndJoin() async throws {
        let (audio, voice, socket, graph) = try await start()
        for sequence in 0..<5 {
            try await push(socket, sequence: UInt32(sequence), bytes: 1920)
            try await Task.sleep(nanoseconds: 40_000_000)
        }
        try await waitFor { graph.native.history.count == 5 }
        let full = await voice.playbackUsage; XCTAssertEqual(full.bytes, 9600)
        XCTAssertEqual(graph.player.retainedBytes, 9600)
        try await push(socket, sequence: 5, bytes: 2)
        try await waitFor { await socket.closed }
        await assertJoined(audio, voice, socket, graph)
        XCTAssertLessThanOrEqual(graph.native.maximumBytes, 9600)
        XCTAssertEqual(graph.native.history.count, 5)
    }
    func testRealPipelinePacketCapIncludesThirtyTwoTinyScheduledReceipts() async throws {
        let (audio, voice, socket, graph) = try await start()
        for sequence in 0..<32 {
            try await push(socket, sequence: UInt32(sequence), bytes: 2)
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        try await waitFor { graph.native.history.count == 32 }
        let full = await voice.playbackUsage; XCTAssertEqual(full.packets, 32)
        XCTAssertEqual(graph.player.retainedPackets, 32)
        try await push(socket, sequence: 32, bytes: 2)
        try await waitFor { await socket.closed }
        await assertJoined(audio, voice, socket, graph)
        XCTAssertEqual(graph.native.maximumPackets, 32)
    }
    func testRealPipelineInterruptionFlushesOldNativePCMAndLateCallbacksCannotFreeNewOutput() async throws {
        let (audio, voice, socket, graph) = try await start()
        for sequence in 0..<2 { try await push(socket, sequence: UInt32(sequence)) }
        try await waitFor { graph.native.history.count == 2 }
        let late = graph.native.history.map(\.completion)
        try await socket.inject("interrupted", ["generation": 1])
        try await waitFor { graph.native.pending.isEmpty }
        try await waitFor { await voice.playbackUsage.bytes == 0 }
        try await socket.inject("generation", ["generation": 2, "phase": "started"])
        try await push(socket, sequence: 0, generation: 2)
        try await waitFor { graph.native.history.count == 3 }
        late.forEach { $0(.consumed) }
        XCTAssertEqual(graph.player.retainedBytes, 960)
        let current = await voice.playbackUsage; XCTAssertEqual(current.bytes, 960)
        await assertJoined(audio, voice, socket, graph)
    }
    func testRealPipelineThrowAfterNativeEnqueueClearsUncertainBufferAndTearsDown() async throws {
        let (audio, voice, socket, graph) = try await start()
        graph.native.failAfterOrdinal = 1
        try await push(socket, sequence: 0); try await push(socket, sequence: 1)
        try await waitFor { await socket.closed }
        await assertJoined(audio, voice, socket, graph)
        XCTAssertEqual(graph.native.history.count, 2)
        XCTAssertGreaterThan(graph.native.clears, 0)
        let failed = await voice.state == .error
        XCTAssertTrue(failed || audio.pauseReason == .playbackFailure)
        graph.native.history.forEach { $0.completion(.consumed) }
        XCTAssertEqual(graph.player.retainedBytes, 0)
    }
    func testRealPipelineConsumptionFailureClearsOtherScheduledPCMAndNeverClaimsSuccess() async throws {
        let (audio, voice, socket, graph) = try await start()
        try await push(socket, sequence: 0); try await push(socket, sequence: 1)
        try await waitFor { graph.native.history.count == 2 }
        graph.native.complete(0, outcome: .failed)
        try await waitFor { await socket.closed }
        await assertJoined(audio, voice, socket, graph)
        let failed = await voice.state == .error
        XCTAssertTrue(failed || audio.pauseReason == .playbackFailure)
        graph.native.history.forEach { $0.completion(.consumed) }
        XCTAssertEqual(graph.player.retainedBytes, 0)
    }
    func testRealPipelineAcceptsInitialAndIntermediateSilentGenerationsValidatedByW09() async throws {
        let (audio, voice, socket, graph) = try await start()
        try await socket.inject("interrupted", ["generation": 1])
        try await socket.inject("generation", ["generation": 2, "phase": "started"])
        try await push(socket, sequence: 0, generation: 2)
        try await waitFor { graph.native.history.count == 1 }
        try await socket.inject("generation", ["generation": 2, "phase": "completed"])
        try await socket.inject("generation", ["generation": 3, "phase": "started"])
        try await socket.inject("generation", ["generation": 3, "phase": "completed"])
        try await socket.inject("generation", ["generation": 4, "phase": "started"])
        try await push(socket, sequence: 0, generation: 4)
        try await waitFor { graph.native.history.count == 2 }
        let machine = await voice.machine
        XCTAssertEqual(machine.wire?.generation, 4)
        XCTAssertEqual(graph.player.retainedBytes, 1920)
        let usage = await voice.playbackUsage; XCTAssertEqual(usage.bytes, 1920)
        await assertJoined(audio, voice, socket, graph)
    }
}

import Foundation

enum VoiceUIState: String, Sendable {
    case idle, pairing, connecting, listening, responding, interrupted, reconnecting, stopping, error
}

enum VoicePacket: Equatable, Sendable { case text(Data), binary(Data) }

enum ReceiveEffect: Sendable {
    case event(WireMessage), audio(AudioEnvelope), discard, closed, stopCommand
}


struct SessionMachine: Sendable {
    private(set) var state: VoiceUIState = .idle
    private(set) var wire: WireState?
    private(set) var receiveDrain: WireState?
    private(set) var shutdownPackets = 0
    private(set) var terminal = false
    private(set) var closedReason: String?
    private var startData: Data?
    private var startedAt = 0.0
    private var stopAt: Double?
    private var lastInbound = 0.0
    private var lastActivity = 0.0
    private var expiresAt = 0.0
    private var lastConnection: UInt64 = 0

    mutating func pairing() throws {
        guard state == .idle || state == .error else { throw VoiceFailure.invalidState }; state = .pairing
    }
    mutating func begin(now: Double, reconnect: Bool = false) throws -> Data {
        guard state == .idle || state == .pairing || state == .error || state == .reconnecting else { throw VoiceFailure.invalidState }
        guard now.isFinite, now >= 0 else { throw VoiceFailure.invalidState }
        state = reconnect ? .reconnecting : .connecting; terminal = false; wire = nil; receiveDrain = nil
        shutdownPackets = 0; stopAt = nil; closedReason = nil; startedAt = now; lastInbound = now; lastActivity = now; expiresAt = now + 540
        let start = try WireJSON.control("start", request: "start_0"); startData = start; return start
    }
    mutating func admit(_ data: Data, now: Double) throws {
        guard !terminal, state != .stopping, var current = wire else { throw VoiceFailure.invalidState }
        try current.client(data, now: now); wire = current
        let m = try WireJSON.parse(data, client: true)
        if m.type == "input" { lastActivity = now }
    }
    mutating func admitPCM(_ pcm: Data, now: Double) throws -> AudioEnvelope {
        guard !terminal, state != .stopping, var current = wire,
              current.inputSequence <= UInt64(UInt32.max) else { throw VoiceFailure.invalidState }
        let frame = AudioEnvelope(kind: .input, connection: UInt32(current.connection), generation: 0,
                                  sequence: UInt32(current.inputSequence), pcm: pcm)
        try current.audio(frame, now: now); wire = current; lastActivity = now; return frame
    }
    mutating func stop(now: Double, request: String) throws -> Data? {
        if state == .stopping || terminal { return nil }
        state = .stopping; stopAt = now
        guard var current = wire, current.phase == .active else { terminal = true; return nil }
        let data = try WireJSON.control("stop", request: request, connection: current.connection)
        let snapshot = current
        try current.client(data, now: now)
        receiveDrain = snapshot; wire = current
        return data
    }
    mutating func receive(_ packet: VoicePacket, now: Double) throws -> ReceiveEffect {
        guard !terminal else { throw VoiceFailure.invalidState }
        if let stopAt {
            guard now >= stopAt, now - stopAt < 2 else { throw VoiceFailure.timeout }
            guard shutdownPackets < 32, var drain = receiveDrain else { throw VoiceFailure.limitExceeded }
            let result: ReceiveEffect
            switch packet {
            case .binary(let data): try drain.audio(AudioEnvelope.decode(data, expected: .output), now: now); result = .discard
            case .text(let data):
                let m = try WireJSON.parse(data); try drain.server(m)
                if m.type == "closed" { terminal = true; wire = drain; state = .idle; closedReason = try m.text("reason"); result = .closed }
                else if m.type == "error", try m.flag("fatal") { throw VoiceFailure.transport }
                else { result = .discard }
            }
            receiveDrain = drain; shutdownPackets += 1; lastInbound = now; return result
        }
        switch packet {
        case .binary(let data):
            guard var current = wire else { throw VoiceFailure.invalidState }
            let frame = try AudioEnvelope.decode(data, expected: .output)
            try current.audio(frame, now: now); wire = current; lastInbound = now
            return .audio(frame)
        case .text(let data):
            let m = try WireJSON.parse(data)
            if wire == nil {
                let connection = try m.uint("connection_generation")
                guard connection > lastConnection, let startData else { throw VoiceFailure.staleGeneration }
                var initial = try WireState(connection: connection); try initial.client(startData, now: startedAt)
                wire = initial
            }
            var current = wire!; try current.server(m); wire = current; lastInbound = now
            lastConnection = max(lastConnection, current.connection)
            switch m.type {
            case "ready":
                guard now - startedAt <= 5 else { throw VoiceFailure.timeout }
                expiresAt = min(expiresAt, now + Double(try m.uint("expires_in_ms")) / 1000)
                state = .listening
            case "generation": state = try m.text("phase") == "started" ? .responding : .listening
            case "interrupted": state = .interrupted
            case "closed": terminal = true; state = .idle; closedReason = try m.text("reason"); return .closed
            case "error": if try m.flag("fatal") { throw VoiceFailure.transport }
            case "transcript":


                if try m.text("speaker") == "user", try m.flag("final"), try m.text("text") == "закончить разговор" {
                    return .stopCommand
                }
            default: break
            }
            return .event(m)
        }
    }
    func checkTime(now: Double) throws {
        guard now.isFinite, now >= startedAt else { throw VoiceFailure.invalidState }
        if terminal { return }
        if let stopAt { if now - stopAt >= 2 { throw VoiceFailure.timeout }; return }
        if wire == nil && now - startedAt >= 5 { throw VoiceFailure.timeout }
        if now >= expiresAt || now - lastInbound >= 30 || now - lastActivity >= 120 { throw VoiceFailure.timeout }
    }
    mutating func fail() { terminal = true; state = .error; wire = nil; receiveDrain = nil; startData = nil }
    mutating func disconnected() { fail() }


    mutating func cleanupCompleted(failed: Bool) {
        terminal = true; state = failed ? .error : .idle
        wire = nil; receiveDrain = nil; startData = nil; stopAt = nil

    }
    mutating func resetForReconnect() {

        fail(); state = .reconnecting
    }
}

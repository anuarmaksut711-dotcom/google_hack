import Foundation

struct TokenBucket: Sendable {
    let rate: Double
    let capacity: Double
    private var tokens: Double
    private var last: Double?
    init(rate: Double, capacity: Double) { self.rate = rate; self.capacity = capacity; tokens = capacity }
    mutating func consume(_ amount: Int, now: Double) throws {
        guard now.isFinite, now >= 0, last == nil || now >= last! else { throw VoiceFailure.invalidState }
        let available = min(capacity, tokens + (last.map { now - $0 } ?? 0) * rate)
        guard available + 1e-7 >= Double(amount) else { throw VoiceFailure.rateLimited }
        tokens = available - Double(amount); last = now
    }
}

struct WireState: Sendable {
    enum Phase: Sendable { case new, starting, active, closing, closed }
    struct Transcript: Sendable {
        let speaker: String
        let generation: UInt64
        let revision: UInt64
        let final: Bool
        let text: String
    }
    struct Tool: Sendable { let name: String; var done: Bool }
    let connection: UInt64
    private(set) var phase: Phase = .new
    private(set) var generation: UInt64 = 0
    private(set) var outputOpen = false
    private(set) var inputOpen = false
    private(set) var muted = false
    private(set) var inputSequence: UInt64 = 0
    private(set) var outputSequence: UInt64 = 0
    private(set) var eventSequence: UInt64 = 0
    private(set) var elapsed: UInt64 = 0
    private(set) var transcripts: [String: Transcript] = [:]
    private(set) var tools: [String: Tool] = [:]
    private(set) var heartbeats: [String] = []
    private var inputBytes = TokenBucket(rate: 32000, capacity: 2560)
    private var outputBytes = TokenBucket(rate: 48000, capacity: 3840)
    private var inputPackets = TokenBucket(rate: 100, capacity: 10)
    private var outputPackets = TokenBucket(rate: 100, capacity: 10)
    private var controls = TokenBucket(rate: 20, capacity: 20)

    private var eventIDs: Set<String> = []

    init(connection: UInt64) throws {
        guard (1...0xFFFFFFFF).contains(connection) else { throw VoiceFailure.staleGeneration }
        self.connection = connection
    }

    mutating func client(_ data: Data, now: Double) throws {
        var next = self; try next.acceptClient(WireJSON.parse(data, client: true), now: now); self = next
    }
    private mutating func acceptClient(_ m: WireMessage, now: Double) throws {
        try controls.consume(1, now: now)
        if m.type == "start" {
            guard phase == .new else { throw VoiceFailure.invalidState }; phase = .starting; return
        }
        guard try m.uint("connection_generation") == connection else { throw VoiceFailure.staleGeneration }
        guard phase == .active else { throw VoiceFailure.invalidState }
        switch m.type {
        case "stop": phase = .closing; inputOpen = false; outputOpen = false
        case "input":
            let desired = try m.text("action") == "begin"
            guard desired != inputOpen else { throw VoiceFailure.invalidState }; inputOpen = desired
        case "mute": muted = try m.flag("muted")
        case "heartbeat":
            guard heartbeats.count < 32 else { throw VoiceFailure.limitExceeded }
            let id = try m.text("request_id")
            guard !heartbeats.contains(id) else { throw VoiceFailure.invalidSequence }; heartbeats.append(id)
        default: throw VoiceFailure.invalidState
        }
    }
    mutating func server(_ m: WireMessage) throws {
        guard m.serverValidated else { throw VoiceFailure.invalidMessage }
        var next = self; try next.acceptServer(m); self = next
    }
    private mutating func output(_ value: UInt64) throws {
        guard outputOpen, generation == value else { throw VoiceFailure.staleGeneration }
    }
    private mutating func acceptServer(_ m: WireMessage) throws {
        guard try m.uint("connection_generation") == connection else { throw VoiceFailure.staleGeneration }
        guard try m.uint("event_sequence") == eventSequence, try m.uint("elapsed_ms") >= elapsed else { throw VoiceFailure.invalidSequence }
        let eventID = try m.text("event_id")
        guard !eventIDs.contains(eventID) else { throw VoiceFailure.invalidSequence }
        guard eventIDs.count < 65536 else { throw VoiceFailure.limitExceeded }
        guard phase != .new, phase != .closed else { throw VoiceFailure.invalidState }
        if m.type == "closed" {
            phase = .closed; inputOpen = false; outputOpen = false; heartbeats.removeAll()
        } else if m.type == "error" {
            let fatal = try m.flag("fatal")
            guard phase != .starting || fatal else { throw VoiceFailure.invalidState }
            if fatal { phase = .closing; inputOpen = false; outputOpen = false }
        } else if m.type == "ready" {
            guard phase == .starting else { throw VoiceFailure.invalidState }; phase = .active; inputOpen = true
        } else {
            guard phase == .active else { throw VoiceFailure.invalidState }
            switch m.type {
            case "generation":
                let value = try m.uint("generation")
                if try m.text("phase") == "started" {
                    guard !outputOpen, value == generation + 1 else { throw VoiceFailure.staleGeneration }
                    generation = value; outputOpen = true; outputSequence = 0
                } else { try output(value); outputOpen = false }
            case "interrupted": try output(m.uint("generation")); outputOpen = false
            case "transcript":
                let speaker = try m.text("speaker"), gen = try m.uint("generation")
                if speaker == "user" { guard gen == 0 else { throw VoiceFailure.staleGeneration } }
                else { try output(gen) }
                let id = try m.text("transcript_id"), revision = try m.uint("revision")
                if let prior = transcripts[id] {
                    guard prior.speaker == speaker, prior.generation == gen, !prior.final, revision == prior.revision + 1 else { throw VoiceFailure.invalidSequence }
                } else {
                    guard revision == 0 else { throw VoiceFailure.invalidSequence }
                    guard transcripts.count < 128 else { throw VoiceFailure.limitExceeded }
                }
                transcripts[id] = Transcript(speaker: speaker, generation: gen, revision: revision,
                                             final: try m.flag("final"), text: try m.text("text"))
            case "tool_started":
                let id = try m.text("call_id")
                guard tools[id] == nil else { throw VoiceFailure.invalidSequence }
                guard tools.count < 64, tools.values.filter({ !$0.done }).count < 4 else { throw VoiceFailure.limitExceeded }
                tools[id] = Tool(name: try m.text("tool"), done: false)
            case "tool_finished":
                let id = try m.text("call_id")
                guard let prior = tools[id], !prior.done, prior.name == (try m.text("tool")) else { throw VoiceFailure.invalidSequence }
                tools[id]?.done = true
            case "heartbeat_ack":
                guard heartbeats.first == (try m.text("request_id")) else { throw VoiceFailure.invalidSequence }
                heartbeats.removeFirst()
            default: throw VoiceFailure.invalidState
            }
        }
        eventIDs.insert(eventID); eventSequence += 1; elapsed = try m.uint("elapsed_ms")
    }
    mutating func audio(_ frame: AudioEnvelope, now: Double) throws {
        var next = self; try next.acceptAudio(frame, now: now); self = next
    }
    private mutating func acceptAudio(_ frame: AudioEnvelope, now: Double) throws {
        try frame.validate()
        guard UInt64(frame.connection) == connection else { throw VoiceFailure.staleGeneration }
        guard phase == .active else { throw VoiceFailure.invalidState }
        if frame.kind == .input {
            guard inputOpen, !muted else { throw VoiceFailure.invalidState }
            guard UInt64(frame.sequence) == inputSequence else { throw VoiceFailure.invalidSequence }
            try inputBytes.consume(frame.pcm.count, now: now); try inputPackets.consume(1, now: now); inputSequence += 1
        } else {
            try output(UInt64(frame.generation))
            guard UInt64(frame.sequence) == outputSequence else { throw VoiceFailure.invalidSequence }
            try outputBytes.consume(frame.pcm.count, now: now); try outputPackets.consume(1, now: now); outputSequence += 1
        }
    }
}

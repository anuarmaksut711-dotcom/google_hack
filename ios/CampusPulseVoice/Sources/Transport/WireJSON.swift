import Foundation

enum VoiceFailure: String, Error, Sendable {
    case invalidJSON, invalidMessage, unsupportedVersion, unknownEvent, messageTooLarge
    case invalidFrame, invalidLength, staleGeneration, invalidSequence, invalidState
    case rateLimited, limitExceeded, backpressure, timeout, transport, configuration
    case pairingDenied, credentialUnavailable
}


indirect enum WireValue: Equatable, Sendable {
    case string(String), number(String), bool(Bool), object([String: WireValue])
    case array([WireValue]), null
}

struct StrictJSON {
    private var bytes: [UInt8]
    private var index = 0
    static func object(_ data: Data, maximum: Int) throws -> [String: WireValue] {
        guard data.count <= maximum else { throw VoiceFailure.messageTooLarge }
        var parser = StrictJSON(bytes: Array(data))
        let value = try parser.value(depth: 0)
        parser.space()
        guard parser.index == parser.bytes.count, case .object(let result) = value else {
            throw VoiceFailure.invalidJSON
        }
        return result
    }
    private mutating func space() {
        while index < bytes.count && [9, 10, 13, 32].contains(bytes[index]) { index += 1 }
    }
    private mutating func take(_ byte: UInt8) throws {
        guard index < bytes.count, bytes[index] == byte else { throw VoiceFailure.invalidJSON }
        index += 1
    }
    private mutating func hex4() throws -> UInt32 {
        var n: UInt32 = 0
        for _ in 0..<4 {
            guard index < bytes.count else { throw VoiceFailure.invalidJSON }
            let b = bytes[index]; index += 1
            let digit: UInt32
            switch b {
            case 48...57: digit = UInt32(b - 48)
            case 65...70: digit = UInt32(b - 55)
            case 97...102: digit = UInt32(b - 87)
            default: throw VoiceFailure.invalidJSON
            }
            n = (n << 4) | digit
        }
        return n
    }
    private mutating func string() throws -> String {
        let start = index
        try take(34)
        while index < bytes.count {
            let b = bytes[index]; index += 1
            if b == 34 {
                do { return try JSONDecoder().decode(String.self, from: Data(bytes[start..<index])) }
                catch { throw VoiceFailure.invalidJSON }
            }
            guard b >= 32 else { throw VoiceFailure.invalidJSON }
            if b == 92 {
                guard index < bytes.count else { throw VoiceFailure.invalidJSON }
                let escaped = bytes[index]; index += 1
                if escaped == 117 {
                    let scalar = try hex4()
                    if (0xD800...0xDBFF).contains(scalar) {
                        try take(92); try take(117)
                        guard (0xDC00...0xDFFF).contains(try hex4()) else { throw VoiceFailure.invalidJSON }
                    } else if (0xDC00...0xDFFF).contains(scalar) { throw VoiceFailure.invalidJSON }
                } else if ![34, 47, 92, 98, 102, 110, 114, 116].contains(escaped) {
                    throw VoiceFailure.invalidJSON
                }
            }
        }
        throw VoiceFailure.invalidJSON
    }
    private mutating func value(depth: Int) throws -> WireValue {
        guard depth <= 32 else { throw VoiceFailure.invalidJSON }
        space()
        guard index < bytes.count else { throw VoiceFailure.invalidJSON }
        switch bytes[index] {
        case 34: return .string(try string())
        case 123:
            index += 1; space()
            var result: [String: WireValue] = [:]
            if index < bytes.count && bytes[index] == 125 { index += 1; return .object(result) }
            while true {
                space(); let key = try string(); space(); try take(58)
                guard result[key] == nil else { throw VoiceFailure.invalidJSON }
                result[key] = try value(depth: depth + 1); space()
                guard index < bytes.count else { throw VoiceFailure.invalidJSON }
                if bytes[index] == 125 { index += 1; return .object(result) }
                try take(44)
            }
        case 91:
            index += 1; space(); var result: [WireValue] = []
            if index < bytes.count && bytes[index] == 93 { index += 1; return .array(result) }
            while true {
                result.append(try value(depth: depth + 1)); space()
                guard index < bytes.count else { throw VoiceFailure.invalidJSON }
                if bytes[index] == 93 { index += 1; return .array(result) }
                try take(44)
            }
        case 116, 102, 110:
            for (word, result) in [("true", WireValue.bool(true)), ("false", .bool(false)), ("null", .null)] {
                let literal = Array(word.utf8)
                if bytes[index...].starts(with: literal) { index += literal.count; return result }
            }
            throw VoiceFailure.invalidJSON
        default:
            let start = index
            while index < bytes.count && ![9, 10, 13, 32, 44, 93, 125].contains(bytes[index]) { index += 1 }
            let token = String(decoding: bytes[start..<index], as: UTF8.self)
            guard token.range(of: #"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$"#, options: .regularExpression) != nil else {
                throw VoiceFailure.invalidJSON
            }
            return .number(token)
        }
    }
}

struct WireMessage: Sendable {
    let fields: [String: WireValue]
    let serverValidated: Bool
    init(fields: [String: WireValue]) { self.fields = fields; serverValidated = false }
    fileprivate init(serverFields: [String: WireValue]) { fields = serverFields; serverValidated = true }
    var type: String { if case .string(let s)? = fields["type"] { return s }; return "" }
    func text(_ key: String) throws -> String {
        guard case .string(let s)? = fields[key] else { throw VoiceFailure.invalidMessage }; return s
    }
    func uint(_ key: String, minimum: UInt64 = 0, maximum: UInt64 = 0xFFFFFFFF) throws -> UInt64 {
        guard case .number(let token)? = fields[key] else { throw VoiceFailure.invalidMessage }
        let s = token == "-0" ? "0" : token
        guard !s.isEmpty, s.utf8.allSatisfy({ (48...57).contains($0) }),
              let n = UInt64(s), n >= minimum, n <= maximum else { throw VoiceFailure.invalidMessage }
        return n
    }
    func flag(_ key: String) throws -> Bool {
        guard case .bool(let b)? = fields[key] else { throw VoiceFailure.invalidMessage }; return b
    }
    func id(_ key: String) throws {
        let s = try text(key)
        guard (1...64).contains(s.utf8.count), s.utf8.allSatisfy({
            (65...90).contains($0) || (97...122).contains($0) || (48...57).contains($0) || $0 == 45 || $0 == 95
        }) else { throw VoiceFailure.invalidMessage }
    }
    func choice(_ key: String, _ values: Set<String>) throws {
        guard values.contains(try text(key)) else { throw VoiceFailure.invalidMessage }
    }
}

enum WireJSON {
    static let inputFormat = "pcm_s16le_16000_mono"
    static let outputFormat = "pcm_s16le_24000_mono"
    static let errorCodes: Set<String> = ["invalid_json", "unsupported_version", "unknown_event", "invalid_message",
        "message_too_large", "invalid_frame", "invalid_length", "invalid_format", "stale_generation", "invalid_sequence",
        "invalid_state", "rate_limited", "limit_exceeded", "access_denied", "access_expired", "revoked",
        "upstream_unavailable", "timeout", "backpressure", "internal_error"]
    static func parse(_ data: Data, client: Bool = false) throws -> WireMessage {
        let message = WireMessage(fields: try StrictJSON.object(data, maximum: client ? 2048 : 32768))
        guard message.fields["version"] == .string("1") else { throw VoiceFailure.unsupportedVersion }
        var keys: Set<String> = ["version", "type"]
        if client {
            keys.insert("request_id"); try message.id("request_id")
            if message.type != "start" { keys.insert("connection_generation"); _ = try message.uint("connection_generation", minimum: 1) }
            switch message.type {
            case "start": keys.formUnion(["input_format", "output_format"]); try formats(message)
            case "input": keys.insert("action"); try message.choice("action", ["begin", "end"])
            case "mute": keys.insert("muted"); _ = try message.flag("muted")
            case "stop", "heartbeat": break
            default: throw VoiceFailure.unknownEvent
            }
        } else {
            keys.formUnion(["event_id", "event_sequence", "connection_generation", "elapsed_ms"])
            try message.id("event_id"); _ = try message.uint("event_sequence")
            _ = try message.uint("connection_generation", minimum: 1); _ = try message.uint("elapsed_ms", maximum: 540000)
            switch message.type {
            case "ready":
                keys.formUnion(["session_id", "run_id", "expires_in_ms", "input_format", "output_format"])
                try message.id("session_id"); try message.id("run_id"); try formats(message)
                _ = try message.uint("expires_in_ms", minimum: 1, maximum: 540000)
            case "generation":
                keys.formUnion(["generation", "phase"]); _ = try message.uint("generation", minimum: 1)
                try message.choice("phase", ["started", "completed"])
            case "interrupted": keys.insert("generation"); _ = try message.uint("generation", minimum: 1)
            case "transcript":
                keys.formUnion(["generation", "transcript_id", "speaker", "revision", "final", "text"])
                _ = try message.uint("generation"); try message.id("transcript_id")
                try message.choice("speaker", ["user", "assistant"]); _ = try message.uint("revision"); _ = try message.flag("final")
                guard (1...4096).contains(try message.text("text").unicodeScalars.count) else { throw VoiceFailure.invalidMessage }
            case "tool_started", "tool_finished":
                keys.formUnion(["call_id", "tool"]); try message.id("call_id")
                try message.choice("tool", ["read_assignments", "read_calendar", "solve_repair"])
                if message.type == "tool_finished" {
                    keys.formUnion(["status", "duration_ms"])
                    try message.choice("status", ["success", "failed", "unavailable", "cancelled"])
                    _ = try message.uint("duration_ms", maximum: 540000)
                }
            case "heartbeat_ack": keys.insert("request_id"); try message.id("request_id")
            case "error": keys.formUnion(["code", "fatal"]); try message.choice("code", errorCodes); _ = try message.flag("fatal")
            case "closed":
                keys.insert("reason"); try message.choice("reason", ["stopped", "expired", "revoked", "disconnected", "timeout", "upstream_error", "protocol_error", "backpressure"])
            default: throw VoiceFailure.unknownEvent
            }
        }
        guard Set(message.fields.keys) == keys else { throw VoiceFailure.invalidMessage }
        return client ? message : WireMessage(serverFields: message.fields)
    }
    private static func formats(_ m: WireMessage) throws {
        try m.choice("input_format", [inputFormat]); try m.choice("output_format", [outputFormat])
    }
    static func control(_ type: String, request: String, connection: UInt64? = nil,
                        action: String? = nil, muted: Bool? = nil) throws -> Data {
        var fields: [String: Any] = ["version": "1", "type": type, "request_id": request]
        if type == "start" { fields["input_format"] = inputFormat; fields["output_format"] = outputFormat }
        if let connection { fields["connection_generation"] = connection }
        if let action { fields["action"] = action }
        if let muted { fields["muted"] = muted }
        let result = try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys, .withoutEscapingSlashes])
        _ = try parse(result, client: true)
        return result
    }
}

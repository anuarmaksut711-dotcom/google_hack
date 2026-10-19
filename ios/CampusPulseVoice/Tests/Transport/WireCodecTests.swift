import XCTest
import CryptoKit
@testable import CampusPulseVoice

// M01 must wire Fixtures/Protocol as test resources (W11 project spec owns this).
enum VoiceFixtures {
    static func data(_ name: String) throws -> Data {
        let parts = name.split(separator: ".", maxSplits: 1).map(String.init)
        let ext = parts.count > 1 ? parts[1] : nil
        guard let url = Bundle(for: WireCodecTests.self).url(forResource: parts[0], withExtension: ext, subdirectory: "Protocol") else {
            throw VoiceFailure.configuration
        }
        return try Data(contentsOf: url)
    }
    static func messages() throws -> [(Bool, Data)] {
        let fixture = try JSONSerialization.jsonObject(with: data("controls.json")) as! [String: Any]
        return try (fixture["messages"] as! [[String: Any]]).map {
            ($0["direction"] as! String == "client", try JSONSerialization.data(withJSONObject: $0["body"]!))
        }
    }
}

final class WireCodecTests: XCTestCase {
    func testGoldenWholeEnvelopeBytesEndianAndHashes() throws {
        let manifest = try JSONSerialization.jsonObject(with: VoiceFixtures.data("golden.json")) as! [String: Any]
        for vector in manifest["vectors"] as! [[String: Any]] {
            let bytes = try VoiceFixtures.data(vector["file"] as! String)
            let kind: AudioEnvelope.Kind = vector["kind"] as! String == "input" ? .input : .output
            let frame = try AudioEnvelope.decode(bytes, expected: kind)
            XCTAssertEqual(frame.connection, 0x01020304); XCTAssertEqual(frame.sequence, 0x0A0B0C0D)
            XCTAssertEqual(frame.generation, kind == .input ? 0 : 0x11223344)
            XCTAssertEqual(frame.samples, [-32768, -1, 0, 1, 32767, 4660, -4660, 42])
            XCTAssertEqual(try frame.encode(), bytes)
            XCTAssertEqual(SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined(), vector["sha256"] as? String)
        }
    }
    func testMalformedAudioBoundsAndDirection() throws {
        let bytes = try VoiceFixtures.data("output-v1.bin")
        XCTAssertThrowsError(try AudioEnvelope.decode(bytes, expected: .input))
        for offset in [0, 4, 5, 6, 7, 20, 23] {
            var bad = bytes; bad[offset] ^= 0xFF
            XCTAssertThrowsError(try AudioEnvelope.decode(bad, expected: .output))
        }
        for count in [0, 1, 1921, 1922] {
            XCTAssertThrowsError(try AudioEnvelope(kind: .output, connection: 1, generation: 1, sequence: 0, pcm: Data(repeating: 0, count: count)).encode())
        }
        XCTAssertThrowsError(try AudioEnvelope.decode(bytes + Data([0]), expected: .output))
    }
    func testStrictJSONTypesDuplicateKeysSurrogatesAndLimits() throws {
        let good = #"{"version":"1","type":"heartbeat","request_id":"r","connection_generation":1}"#
        _ = try WireJSON.parse(Data(good.utf8), client: true)
        let invalid = [good.replacingOccurrences(of: #""version":"1""#, with: #""version":1"#),
            good.replacingOccurrences(of: #""connection_generation":1"#, with: #""connection_generation":true"#),
            good.replacingOccurrences(of: #""connection_generation":1"#, with: #""connection_generation":1.0"#),
            good.replacingOccurrences(of: #""connection_generation":1"#, with: #""connection_generation":1e0"#),
            good.replacingOccurrences(of: #""connection_generation":1"#, with: #""connection_generation":4294967296"#),
            good.replacingOccurrences(of: "}", with: #","approved":true}"#),
            good.replacingOccurrences(of: "}", with: #","type":"stop"}"#),
            good.replacingOccurrences(of: #""request_id":"r""#, with: #""request_id":"\ud800""#),
            good.replacingOccurrences(of: #""request_id":"r""#, with: #""request_id":"\udc00""#),
            good.replacingOccurrences(of: #""connection_generation":1"#, with: #""connection_generation":NaN"#)]
        for text in invalid { XCTAssertThrowsError(try WireJSON.parse(Data(text.utf8), client: true)) }
        XCTAssertThrowsError(try WireJSON.parse(Data([0xFF]), client: true))
        XCTAssertThrowsError(try WireJSON.parse(Data(repeating: 32, count: 2049), client: true))
    }
    func testControlsGoldenTraceAndIndependentCounters() throws {
        var state = try WireState(connection: 0x01020304)
        for (client, data) in try VoiceFixtures.messages() {
            if client { try state.client(data, now: 0.5) } else { try state.server(WireJSON.parse(data)) }
        }
        XCTAssertEqual(state.phase, .closed); XCTAssertEqual(state.eventSequence, 13)
        XCTAssertEqual(state.inputSequence, 0); XCTAssertEqual(state.outputSequence, 0)
        XCTAssertEqual(state.transcripts["tr_user"]?.text, "Синтетический вопрос")
        XCTAssertEqual(state.transcripts["tr_user"]?.revision, 1)
        XCTAssertEqual(state.transcripts["tr_assistant_1"]?.final, false)
    }
    func testRFC7636S256Proof() {
        let proof = PairingProof(verifier: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        XCTAssertEqual(proof.challenge, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
        XCTAssertFalse(String(describing: proof).contains(proof.verifier))
    }
    func testConfiguredOriginRejectsCredentialURLsAndDifferentPorts() throws {
        let origin = try BackendOrigin("https://voice.example.invalid:8443")
        XCTAssertEqual(try origin.url(path: "/voice/pair").absoluteString, "https://voice.example.invalid:8443/voice/pair")
        for bad in ["http://voice.example.invalid", "https://user:secret@voice.example.invalid", "https://voice.example.invalid?token=x",
                    "https://voice.example.invalid/path", "https://voice.example.invalid#secret", "https://voice.example.invalid:0", "https://voice.example.invalid/%2f"] {
            XCTAssertThrowsError(try BackendOrigin(bad))
        }
        XCTAssertFalse(origin.matches(URL(string: "https://voice.example.invalid")!, websocket: false))
        XCTAssertFalse(origin.matches(URL(string: "https://other.invalid:8443")!, websocket: false))
        let c = NativeConfiguration.session()
        XCTAssertNil(c.httpCookieStorage); XCTAssertFalse(c.httpShouldSetCookies); XCTAssertNil(c.urlCredentialStorage)
    }
}

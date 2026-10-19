import Foundation
import XCTest
@testable import CampusPulseVoice

actor AppTestPairingHTTP: PairingHTTP {
    private(set) var paths: [String] = []
    var deny = false
    func setDenied() { deny = true }
    func post(path: String, body: Data) async throws -> Data {
        paths.append(path)
        if deny { throw VoiceFailure.pairingDenied }
        let expiry = ISO8601DateFormatter().string(from: Date().addingTimeInterval(120))
        if path == "/api/voice/pairing/start" {
            return try JSONSerialization.data(withJSONObject: ["pairing_code": "ABCDEFGHJKLM", "grant": String(repeating: "g", count: 43),
                "expires_at": expiry, "approval_path": "/voice/pair"])
        }
        guard path == "/api/voice/pairing/exchange" else { throw VoiceFailure.pairingDenied }
        return try JSONSerialization.data(withJSONObject: ["credential": String(repeating: "c", count: 43),
            "expires_at": expiry, "scope": "voice:read"])
    }
}

@MainActor final class AppTestPairingBrowser: PairingBrowser {
    var pages: [URL] = []
    func openVerificationPage(_ url: URL) async throws { pages.append(url) }
}

@MainActor final class AppPairingTests: XCTestCase {
    private func settle(_ model: VoiceScreenModel) async throws {
        for _ in 0..<500 {
            if !model.pending && !model.stopping { return }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        XCTFail("Pairing action did not settle"); throw VoiceFailure.timeout
    }
    func testFixedBrowserPageExplicitContinueAndNoAudioAdoption() async throws {
        let http = AppTestPairingHTTP(), browser = AppTestPairingBrowser(), store = AppTestStore(nil)
        var made = 0
        let model = VoiceScreenModel(origin: "https://voice.campus.edu", store: store, foreground: { true }, factory: { claim, observer in
            made += 1; return AppTestConversation(claim: claim, observer: observer)
        }, pairingFactory: { DevicePairing(origin: $0, http: http, store: store, browser: browser) })
        model.pairDevice(); try await settle(model)
        XCTAssertEqual(model.pairing?.code, "ABCDEFGHJKLM")
        XCTAssertEqual(browser.pages.first?.path, "/voice/pair"); XCTAssertNil(browser.pages.first?.query)
        XCTAssertNil(browser.pages.first?.fragment)
        let before = await http.paths; XCTAssertEqual(before, ["/api/voice/pairing/start"])
        XCTAssertEqual(made, 0); XCTAssertFalse(model.conversationActive)
        model.continuePairing(); try await settle(model)
        let after = await http.paths; XCTAssertEqual(after, ["/api/voice/pairing/start", "/api/voice/pairing/exchange"])
        XCTAssertNil(model.pairing); XCTAssertTrue(model.canStart); XCTAssertEqual(made, 0)
        XCTAssertNotNil(try store.load(for: BackendOrigin("https://voice.campus.edu")))
    }
    func testDeniedPairingFailsWithoutCredentialOrAudio() async throws {
        let http = AppTestPairingHTTP(), store = AppTestStore(nil), browser = AppTestPairingBrowser()
        await http.setDenied()
        let model = VoiceScreenModel(origin: "https://voice.campus.edu", store: store, foreground: { true },
            pairingFactory: { DevicePairing(origin: $0, http: http, store: store, browser: browser) })
        model.pairDevice(); try await settle(model)
        XCTAssertNil(model.pairing); XCTAssertFalse(model.conversationActive)
        XCTAssertEqual(model.message, AppCopy.error(AppIssue.pairing)); XCTAssertTrue(browser.pages.isEmpty)
        XCTAssertNil(try store.load(for: BackendOrigin("https://voice.campus.edu")))
    }
}

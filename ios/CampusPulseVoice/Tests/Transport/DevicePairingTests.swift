import XCTest
import Foundation
@testable import CampusPulseVoice

actor FakePairingHTTP: PairingHTTP {
    var bodies: [Data] = []
    var paths: [String] = []
    private var denyExchange = false
    private var badPath = false
    let clock: Date
    init(clock: Date) { self.clock = clock }
    func configure(deny: Bool = false, badPath: Bool = false) { denyExchange = deny; self.badPath = badPath }
    func post(path: String, body: Data) async throws -> Data {
        paths.append(path); bodies.append(body)
        let formatter = ISO8601DateFormatter()
        if path.hasSuffix("start") {
            return try JSONSerialization.data(withJSONObject: ["pairing_code": "ABCDEFGH2345", "grant": String(repeating: "g", count: 43),
                "expires_at": formatter.string(from: clock.addingTimeInterval(300)),
                "approval_path": badPath ? "https://other.invalid/?grant=forbidden" : "/voice/pair"])
        }
        if denyExchange { throw VoiceFailure.pairingDenied }
        return try JSONSerialization.data(withJSONObject: ["credential": String(repeating: "c", count: 43), "scope": "voice:read",
            "expires_at": formatter.string(from: clock.addingTimeInterval(1200))])
    }
}

final class MemoryCredentialStore: CredentialStore, @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String: DeviceCredential] = [:]
    private func key(_ origin: BackendOrigin) -> String { origin.host + ":" + String(origin.port) }
    func load(for origin: BackendOrigin) throws -> DeviceCredential? {
        lock.lock(); defer { lock.unlock() }; return values[key(origin)]
    }
    func save(_ credential: DeviceCredential, for origin: BackendOrigin) throws {
        lock.lock(); defer { lock.unlock() }; values[key(origin)] = credential
    }
    func remove(for origin: BackendOrigin) throws { lock.lock(); defer { lock.unlock() }; values.removeValue(forKey: key(origin)) }
}

@MainActor final class FakePairingBrowser: PairingBrowser {
    var pages: [URL] = []
    func openVerificationPage(_ url: URL) async throws { pages.append(url) }
}

@MainActor final class DevicePairingTests: XCTestCase {
    func testW03FixedPageS256JSONBodyAndCredentialAbstraction() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let origin = try BackendOrigin("https://voice.example.invalid:8443")
        let http = FakePairingHTTP(clock: now), browser = FakePairingBrowser(), store = MemoryCredentialStore()
        let pairing = DevicePairing(origin: origin, http: http, store: store, browser: browser)
        let presentation = try await pairing.begin(deviceName: "Synthetic iPhone", now: now)
        XCTAssertEqual(presentation.status, .awaitingBrowserReview); XCTAssertEqual(presentation.code, "ABCDEFGH2345")
        XCTAssertEqual(browser.pages, [try origin.url(path: "/voice/pair")])
        XCTAssertNil(try store.load(for: origin))
        try await pairing.continueAfterBrowserApproval(now: now)
        let bodies = await http.bodies, paths = await http.paths
        XCTAssertEqual(paths, ["/api/voice/pairing/start", "/api/voice/pairing/exchange"])
        let start = try StrictJSON.object(bodies[0], maximum: 2048), exchange = try StrictJSON.object(bodies[1], maximum: 2048)
        XCTAssertEqual(Set(start.keys), ["challenge", "client_kind", "device_name"])
        XCTAssertEqual(Set(exchange.keys), ["grant", "verifier"])
        let proof = PairingProof(verifier: try WireMessage(fields: exchange).text("verifier"))
        XCTAssertEqual(start["challenge"], .string(proof.challenge)); XCTAssertEqual(start["client_kind"], .string("iphone"))
        XCTAssertNotNil(try store.load(for: origin))
        XCTAssertNil(try store.load(for: BackendOrigin("https://other.invalid:8443")))
        do { try await pairing.continueAfterBrowserApproval(now: now); XCTFail("one-use exchange retried") } catch {}
        let count = await http.paths.count; XCTAssertEqual(count, 2)
    }
    func testDeniedOrUnknownExchangeIsNotPolledOrRetried() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000), origin = try BackendOrigin("https://voice.example.invalid")
        let http = FakePairingHTTP(clock: now), browser = FakePairingBrowser(), store = MemoryCredentialStore()
        await http.configure(deny: true)
        let pairing = DevicePairing(origin: origin, http: http, store: store, browser: browser)
        _ = try await pairing.begin(deviceName: "Synthetic", now: now)
        for _ in 0..<2 {
            do { try await pairing.continueAfterBrowserApproval(now: now); XCTFail("must deny") } catch {}
        }
        let count = await http.paths.count; XCTAssertEqual(count, 2); XCTAssertNil(try store.load(for: origin))
    }
    func testCancelExpiryAndUntrustedCredentialApprovalURL() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000), origin = try BackendOrigin("https://voice.example.invalid")
        let http = FakePairingHTTP(clock: now), browser = FakePairingBrowser(), store = MemoryCredentialStore()
        let pairing = DevicePairing(origin: origin, http: http, store: store, browser: browser)
        _ = try await pairing.begin(deviceName: "Synthetic", now: now); await pairing.cancel()
        do { try await pairing.continueAfterBrowserApproval(now: now); XCTFail("cancelled attempt exchanged") } catch {}
        _ = try await pairing.begin(deviceName: "Synthetic", now: now)
        do { try await pairing.continueAfterBrowserApproval(now: now.addingTimeInterval(300)); XCTFail("expiry equality accepted") } catch {}
        await http.configure(badPath: true)
        do { _ = try await pairing.begin(deviceName: "Synthetic", now: now); XCTFail("untrusted URL accepted") } catch {}
        XCTAssertEqual(browser.pages.count, 2); XCTAssertNil(try store.load(for: origin))
    }
    func testRedirectDelegateRejectsEvenSameHostWithoutForwardingCredentials() throws {
        let delegate = NoRedirectDelegate(), origin = try BackendOrigin("https://voice.example.invalid")
        let url = try origin.url(path: "/voice/pair")
        let session = URLSession(configuration: NativeConfiguration.session())
        defer { session.invalidateAndCancel() }
        let task = session.dataTask(with: url)
        let response = HTTPURLResponse(url: url, statusCode: 302, httpVersion: "HTTP/1.1", headerFields: nil)!
        for destination in [url, URL(string: "https://other.invalid")!] {
            let completed = expectation(description: "redirect denied")
            delegate.urlSession(session, task: task, willPerformHTTPRedirection: response, newRequest: URLRequest(url: destination)) {
                XCTAssertNil($0); completed.fulfill()
            }
        }
        waitForExpectations(timeout: 0.1)
    }
}

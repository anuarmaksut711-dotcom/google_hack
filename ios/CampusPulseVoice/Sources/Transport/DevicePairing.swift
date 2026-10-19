import Foundation
import CryptoKit
import Security

struct DeviceCredential: Sendable, CustomStringConvertible, CustomReflectable {
    let token: String
    let expiresAt: Date
    init(token: String, expiresAt: Date) throws {
        guard token.range(of: #"^[A-Za-z0-9_-]{43}$"#, options: .regularExpression) != nil else { throw VoiceFailure.credentialUnavailable }
        self.token = token; self.expiresAt = expiresAt
    }
    var description: String { "DeviceCredential(redacted)" }
    var customMirror: Mirror { Mirror(self, children: ["status": "redacted"]) }
}



struct VoiceClaim: Equatable, Sendable, CustomStringConvertible, CustomReflectable {
    let origin: BackendOrigin
    let expiresAt: Date
    private let credentialDigest: Data
    init(origin: BackendOrigin, credential: DeviceCredential) {
        self.origin = origin; expiresAt = credential.expiresAt
        credentialDigest = Data(SHA256.hash(data: Data(credential.token.utf8)))
    }
    var description: String { "VoiceClaim(redacted)" }
    var customMirror: Mirror { Mirror(self, children: ["status": "redacted"]) }
}

protocol CredentialStore: Sendable {
    func load(for origin: BackendOrigin) throws -> DeviceCredential?
    func save(_ credential: DeviceCredential, for origin: BackendOrigin) throws
    func remove(for origin: BackendOrigin) throws
}

struct KeychainCredentialStore: CredentialStore {
    private let service = "CampusPulseVoice.device.voice-read.v1"
    private func query(_ origin: BackendOrigin) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
         kSecAttrAccount as String: origin.host + ":" + String(origin.port), kSecAttrSynchronizable as String: false]
    }
    func save(_ credential: DeviceCredential, for origin: BackendOrigin) throws {
        let data = try JSONSerialization.data(withJSONObject: ["token": credential.token, "expires": credential.expiresAt.timeIntervalSince1970])
        let attributes: [String: Any] = [kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        let base = query(origin)
        let updated = SecItemUpdate(base as CFDictionary, attributes as CFDictionary)
        if updated == errSecItemNotFound {
            var add = base; attributes.forEach { add[$0.key] = $0.value }
            guard SecItemAdd(add as CFDictionary, nil) == errSecSuccess else { throw VoiceFailure.credentialUnavailable }
        } else if updated != errSecSuccess { throw VoiceFailure.credentialUnavailable }
    }
    func load(for origin: BackendOrigin) throws -> DeviceCredential? {
        var q = query(origin); q[kSecReturnData as String] = true; q[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = item as? Data,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let token = json["token"] as? String, let expires = json["expires"] as? Double, expires.isFinite else {
            throw VoiceFailure.credentialUnavailable
        }
        return try DeviceCredential(token: token, expiresAt: Date(timeIntervalSince1970: expires))
    }
    func remove(for origin: BackendOrigin) throws {
        let status = SecItemDelete(query(origin) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw VoiceFailure.credentialUnavailable }
    }
}

@MainActor protocol PairingBrowser: Sendable { func openVerificationPage(_ url: URL) async throws }

struct PairingPresentation: Sendable {
    enum Status: String, Sendable { case awaitingBrowserReview, exchanging, paired, cancelled, deniedOrExpired }
    let code: String
    let expiresAt: Date
    let verificationPage: URL
    let status: Status
}

struct PairingProof: Sendable, CustomStringConvertible, CustomReflectable {
    let verifier: String
    let challenge: String
    init() throws {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else { throw VoiceFailure.pairingDenied }
        self.init(verifier: Self.base64url(Data(bytes)))
    }

    init(verifier: String) {
        self.verifier = verifier
        challenge = Self.base64url(Data(SHA256.hash(data: Data(verifier.utf8))))
    }
    static func base64url(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    }
    var description: String { "PairingProof(redacted)" }
    var customMirror: Mirror { Mirror(self, children: ["status": "redacted"]) }
}

actor DevicePairing {
    private struct Attempt { let proof: PairingProof; let grant: String; let expires: Date }
    private let origin: BackendOrigin
    private let http: any PairingHTTP
    private let store: any CredentialStore
    private let browser: any PairingBrowser
    private var attempt: Attempt?
    private var epoch: UInt64 = 0
    private var busy = false
    private(set) var status: PairingPresentation.Status = .cancelled
    init(origin: BackendOrigin, http: any PairingHTTP, store: any CredentialStore, browser: any PairingBrowser) {
        self.origin = origin; self.http = http; self.store = store; self.browser = browser
    }
    func cancel() { epoch += 1; attempt = nil; status = .cancelled }
    func begin(deviceName: String, now: Date = Date()) async throws -> PairingPresentation {
        guard !busy, attempt == nil, (1...80).contains(deviceName.unicodeScalars.count) else { throw VoiceFailure.invalidState }
        busy = true; epoch += 1; let ticket = epoch
        defer { busy = false }
        do {
            let proof = try PairingProof()
            let body = try JSONSerialization.data(withJSONObject: ["challenge": proof.challenge, "client_kind": "iphone", "device_name": deviceName])
            let data = try await http.post(path: "/api/voice/pairing/start", body: body)
            try Task.checkCancellation()
            guard ticket == epoch else { throw VoiceFailure.pairingDenied }
            let m = WireMessage(fields: try StrictJSON.object(data, maximum: 4096))
            guard Set(m.fields.keys) == ["pairing_code", "grant", "expires_at", "approval_path"],
                  try m.text("approval_path") == "/voice/pair" else { throw VoiceFailure.pairingDenied }
            let code = try m.text("pairing_code"), grant = try m.text("grant")
            guard code.range(of: #"^[A-HJ-NP-Z2-9]{12}$"#, options: .regularExpression) != nil,
                  grant.range(of: #"^[A-Za-z0-9_-]{43}$"#, options: .regularExpression) != nil else { throw VoiceFailure.pairingDenied }
            let expires = try Self.date(m.text("expires_at"))
            guard expires > now, expires <= now.addingTimeInterval(301) else { throw VoiceFailure.pairingDenied }
            let page = try origin.url(path: "/voice/pair")
            attempt = Attempt(proof: proof, grant: grant, expires: expires); status = .awaitingBrowserReview
            try await browser.openVerificationPage(page)
            try Task.checkCancellation()
            guard ticket == epoch else { throw VoiceFailure.pairingDenied }
            return PairingPresentation(code: code, expiresAt: expires, verificationPage: page, status: status)
        } catch {
            if ticket == epoch { attempt = nil; status = .deniedOrExpired }
            throw VoiceFailure.pairingDenied
        }
    }


    func continueAfterBrowserApproval(now: Date = Date()) async throws {
        guard !busy, let value = attempt, value.expires > now else { attempt = nil; throw VoiceFailure.pairingDenied }
        busy = true; attempt = nil; status = .exchanging; let ticket = epoch
        defer { busy = false }
        do {
            let body = try JSONSerialization.data(withJSONObject: ["grant": value.grant, "verifier": value.proof.verifier])
            let response = try await http.post(path: "/api/voice/pairing/exchange", body: body)
            try Task.checkCancellation()
            guard ticket == epoch else { throw VoiceFailure.pairingDenied }
            let m = WireMessage(fields: try StrictJSON.object(response, maximum: 4096))
            guard Set(m.fields.keys) == ["credential", "expires_at", "scope"], try m.text("scope") == "voice:read" else { throw VoiceFailure.pairingDenied }
            let expiry = try Self.date(m.text("expires_at"))
            guard expiry > now, expiry <= now.addingTimeInterval(1201) else { throw VoiceFailure.pairingDenied }
            let credential = try DeviceCredential(token: m.text("credential"), expiresAt: expiry)
            try store.save(credential, for: origin); status = .paired
        } catch {
            if ticket == epoch { status = .deniedOrExpired }

            throw VoiceFailure.pairingDenied
        }
    }
    private static func date(_ value: String) throws -> Date {
        let formatter = ISO8601DateFormatter(); formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let result = formatter.date(from: value) { return result }
        formatter.formatOptions = [.withInternetDateTime]
        guard let result = formatter.date(from: value) else { throw VoiceFailure.pairingDenied }; return result
    }
}

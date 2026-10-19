import Foundation

struct BackendOrigin: Equatable, Sendable {
    let host: String
    let port: Int
    private let components: URLComponents
    init(_ configuredOrigin: String) throws {
        guard !configuredOrigin.contains(where: { $0.isWhitespace }), !configuredOrigin.contains("%"),
              let c = URLComponents(string: configuredOrigin), c.scheme == "https",
              let host = c.host, !host.isEmpty, c.user == nil, c.password == nil,
              c.query == nil, c.fragment == nil, c.path == "" || c.path == "/",
              (1...65535).contains(c.port ?? 443),
              host.range(of: #"^[A-Za-z0-9.-]+$"#, options: .regularExpression) != nil else { throw VoiceFailure.configuration }
        self.host = host.lowercased(); port = c.port ?? 443
        var normalized = c; normalized.host = self.host; normalized.path = ""; components = normalized
    }
    func url(path: String, websocket: Bool = false) throws -> URL {
        let paths = websocket ? ["/api/voice/ws"] : ["/voice/pair", "/api/voice/pairing/start", "/api/voice/pairing/exchange"]
        guard paths.contains(path) else { throw VoiceFailure.configuration }
        var c = components; c.scheme = websocket ? "wss" : "https"; c.path = path
        guard let url = c.url else { throw VoiceFailure.configuration }; return url
    }
    func matches(_ url: URL, websocket: Bool) -> Bool {
        guard let c = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return false }
        return c.scheme == (websocket ? "wss" : "https") && c.host?.lowercased() == host && (c.port ?? 443) == port
            && c.user == nil && c.password == nil && c.query == nil && c.fragment == nil
    }
}

func bounded<T: Sendable>(seconds: Double, operation: @escaping @Sendable () async throws -> T) async throws -> T {
    try await withThrowingTaskGroup(of: T.self) { group in
        group.addTask { try await operation() }
        group.addTask {
            try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            throw VoiceFailure.timeout
        }
        defer { group.cancelAll() }
        guard let first = try await group.next() else { throw VoiceFailure.transport }; return first
    }
}

// No authentication-challenge override: URLSession performs normal system TLS
// certificate/hostname validation. All redirects are refused, including same-host.
final class NoRedirectDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

enum NativeConfiguration {
    static func session() -> URLSessionConfiguration {
        let c = URLSessionConfiguration.ephemeral
        c.httpCookieStorage = nil; c.httpShouldSetCookies = false; c.httpCookieAcceptPolicy = .never
        c.urlCredentialStorage = nil; c.urlCache = nil; c.requestCachePolicy = .reloadIgnoringLocalCacheData
        c.timeoutIntervalForRequest = 5; c.timeoutIntervalForResource = 7
        c.connectionProxyDictionary = [:]
        c.httpAdditionalHeaders = [:]
        return c
    }
}

protocol PairingHTTP: Sendable { func post(path: String, body: Data) async throws -> Data }

struct NativePairingHTTP: PairingHTTP {
    let origin: BackendOrigin
    func post(path: String, body: Data) async throws -> Data {
        guard ["/api/voice/pairing/start", "/api/voice/pairing/exchange"].contains(path), body.count <= 2048 else { throw VoiceFailure.configuration }
        let url = try origin.url(path: path)
        var request = URLRequest(url: url); request.httpMethod = "POST"; request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpShouldHandleCookies = false
        let finalRequest = request
        let session = URLSession(configuration: NativeConfiguration.session(), delegate: NoRedirectDelegate(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        do {
            return try await bounded(seconds: 7) {
                try await withTaskCancellationHandler {
                    // Streaming collection has a cap BEFORE appending, unlike data(for:).
                    let (stream, response) = try await session.bytes(for: finalRequest)
                    guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                          let finalURL = http.url, origin.matches(finalURL, websocket: false), finalURL.path == path,
                          http.value(forHTTPHeaderField: "Content-Type")?.lowercased().hasPrefix("application/json") == true,
                          response.expectedContentLength <= 4096 else { throw VoiceFailure.pairingDenied }
                    var bytes = Data()
                    for try await byte in stream {
                        guard bytes.count < 4096 else { throw VoiceFailure.messageTooLarge }; bytes.append(byte)
                    }
                    return bytes
                } onCancel: { session.invalidateAndCancel() }
            }
        } catch let error as VoiceFailure { throw error }
        catch { throw VoiceFailure.pairingDenied }
    }
}

protocol VoiceSocket: Sendable {
    var claim: VoiceClaim { get }
    func send(_ packet: VoicePacket) async throws
    func receive() async throws -> VoicePacket
    // Must wake outstanding send/receive and cooperate with task cancellation.
    func close() async
}

final class NativeVoiceSocket: VoiceSocket, @unchecked Sendable {
    let claim: VoiceClaim
    private let task: URLSessionWebSocketTask
    private let session: URLSession
    private let origin: BackendOrigin
    init(origin: BackendOrigin, credential: DeviceCredential) throws {
        guard credential.expiresAt > Date() else { throw VoiceFailure.credentialUnavailable }
        self.origin = origin
        claim = VoiceClaim(origin: origin, credential: credential)
        let url = try origin.url(path: "/api/voice/ws", websocket: true)
        var request = URLRequest(url: url); request.httpShouldHandleCookies = false
        request.setValue("Bearer " + credential.token, forHTTPHeaderField: "Authorization")
        session = URLSession(configuration: NativeConfiguration.session(), delegate: NoRedirectDelegate(), delegateQueue: nil)
        task = session.webSocketTask(with: request)
        task.maximumMessageSize = 32768
        task.resume()
    }
    private func abort() { task.cancel(with: .goingAway, reason: nil); session.invalidateAndCancel() }
    private func validateHandshake() throws {
        guard let response = task.response as? HTTPURLResponse, response.statusCode == 101,
              let url = response.url, origin.matches(url, websocket: true), url.path == "/api/voice/ws",
              response.value(forHTTPHeaderField: "Sec-WebSocket-Extensions") == nil,
              response.value(forHTTPHeaderField: "Sec-WebSocket-Protocol") == nil else { throw VoiceFailure.transport }
    }
    func send(_ packet: VoicePacket) async throws {
        do {
            try await withTaskCancellationHandler {
                switch packet {
                case .text(let data):
                    guard data.count <= 2048, let text = String(data: data, encoding: .utf8) else { throw VoiceFailure.invalidJSON }
                    try await task.send(.string(text))
                case .binary(let data):
                    guard data.count <= 1304 else { throw VoiceFailure.invalidLength }; try await task.send(.data(data))
                }
                try validateHandshake()
            } onCancel: { self.abort() }
        } catch let failure as VoiceFailure { abort(); throw failure }
        catch { abort(); throw VoiceFailure.transport }
    }
    func receive() async throws -> VoicePacket {
        do {
            return try await withTaskCancellationHandler {
                let message = try await task.receive(); try validateHandshake()
                switch message {
                case .string(let s):
                    let data = Data(s.utf8); guard data.count <= 32768 else { throw VoiceFailure.messageTooLarge }; return .text(data)
                case .data(let d): guard d.count <= 1944 else { throw VoiceFailure.invalidLength }; return .binary(d)
                @unknown default: throw VoiceFailure.invalidMessage
                }
            } onCancel: { self.abort() }
        } catch let failure as VoiceFailure { abort(); throw failure }
        catch { abort(); throw VoiceFailure.transport }
    }
    func close() async { abort() }
}

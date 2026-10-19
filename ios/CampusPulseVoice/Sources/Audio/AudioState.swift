import Foundation

// CampusPulse original. Pure, bounded lifecycle seams; no device API here.
enum AudioFailure: Error, Equatable, Sendable {
    case permissionDenied, inactive, retiredTicket, capacity, invalidPCM
    case formatChanged, conversion, graph, admissionTimeout, drainTimeout
    case armingCancelled
}

struct CaptureReady: Equatable, Sendable {
    let ticket: CaptureTicket
    let lifetime: UUID
    let revision: UUID
}

enum CaptureStatus: Equatable, Sendable {
    case inactive, arming(CaptureReady), armed(CaptureReady), ending(CaptureReady), muted(CaptureTicket), failed(AudioFailure)
}

// One bounded UI waiter per arming revision. Success is published only after
// graph.startCapture returns successfully; cancellation/failure cannot later
// turn into ready. Thread-safe to cooperate with structured timeout cancellation.
final class CaptureArming: @unchecked Sendable {
    let identity: CaptureReady
    private let lock = NSLock()
    private var result: Result<CaptureReady, AudioFailure>?
    private var waiter: CheckedContinuation<CaptureReady, Error>?
    private var claimed = false
    init(_ identity: CaptureReady) { self.identity = identity }
    var resolved: Result<CaptureReady, AudioFailure>? {
        lock.lock(); defer { lock.unlock() }; return result
    }
    @discardableResult func resolve(_ value: Result<CaptureReady, AudioFailure>) -> Bool {
        if case .success(let ready) = value, ready != identity { return false }
        lock.lock()
        guard result == nil else { lock.unlock(); return false }
        result = value; let pending = waiter; waiter = nil; lock.unlock()
        switch value {
        case .success(let ready): pending?.resume(returning: ready)
        case .failure(let error): pending?.resume(throwing: error)
        }
        return true
    }
    func wait() async throws -> CaptureReady {
        try await withTaskCancellationHandler {
            let value: CaptureReady = try await withCheckedThrowingContinuation { continuation in
                lock.lock()
                guard !claimed else { lock.unlock(); continuation.resume(throwing: AudioFailure.inactive); return }
                claimed = true
                if let result {
                    lock.unlock()
                    switch result {
                    case .success(let ready): continuation.resume(returning: ready)
                    case .failure(let error): continuation.resume(throwing: error)
                    }
                } else { waiter = continuation; lock.unlock() }
            }
            try Task.checkCancellation(); return value
        } onCancel: { self.resolve(.failure(.armingCancelled)) }
    }
}

enum AudioPauseReason: String, Sendable {
    case interruption, headphonesRemoved, routeChanged, mediaReset, mediaLost
    case engineChanged, overflow, captureFailure, playbackFailure
}

struct AudioFences {
    private(set) var authorized = false
    private(set) var epoch: UUID?
    private(set) var capture: CaptureTicket?
    private var retiredEpochs: [UUID] = []
    private var retiredCaptures: [CaptureTicket] = []
    private var minimumSerial: UInt64 = 0
    private var generation: UInt32 = 0
    private var exhausted = false

    mutating func authorize() throws {
        guard !exhausted, !authorized else { throw AudioFailure.inactive }
        authorized = true
    }
    mutating func adopt(_ value: UUID) throws {
        guard authorized, !exhausted, !retiredEpochs.contains(value),
              epoch == nil || epoch == value else { throw AudioFailure.retiredTicket }
        epoch = value
    }
    mutating func begin(_ ticket: CaptureTicket) throws {
        try adopt(ticket.epoch)
        guard !retiredCaptures.contains(ticket), capture == nil || capture == ticket else {
            throw AudioFailure.retiredTicket
        }
        capture = ticket
    }
    mutating func retire(_ ticket: CaptureTicket) {
        if !retiredCaptures.contains(ticket) {
            if retiredCaptures.count < 1024 { retiredCaptures.append(ticket) }
            else { exhausted = true; authorized = false }
        }
        if capture == ticket { capture = nil }
    }
    mutating func accept(_ ticket: PlaybackTicket) throws {
        try adopt(ticket.epoch)
        guard ticket.serial >= minimumSerial, ticket.generation > 0,
              ticket.generation >= generation else { throw AudioFailure.retiredTicket }
        generation = ticket.generation
    }
    mutating func flush(epoch value: UUID, nextSerial: UInt64) -> Bool {
        do { try adopt(value) } catch { return false }
        // A late/lower flush can never lower the admission floor.
        minimumSerial = max(minimumSerial, nextSerial)
        return true
    }
    mutating func close(_ value: UUID) -> Bool {
        let alreadyRetired = retiredEpochs.contains(value)
        if !alreadyRetired {
            if retiredEpochs.count < 64 { retiredEpochs.append(value) }
            else { exhausted = true; authorized = false }
        }
        guard epoch == value || (epoch == nil && authorized && !alreadyRetired) else { return exhausted }
        if let capture { retire(capture) }
        epoch = nil; authorized = false; minimumSerial = 0; generation = 0
        return true
    }
    mutating func stop() {
        if let epoch { _ = close(epoch) }
        authorized = false
    }
}

struct CapturePacer {
    private var next = 0.0
    private var last = 0.0
    mutating func reserve(bytes: Int, now: Double) throws -> Double {
        guard now.isFinite, now >= last, (2...1280).contains(bytes), bytes % 2 == 0 else {
            throw AudioFailure.invalidPCM
        }
        last = now
        let due = max(now, next)
        next = due + Double(bytes) / 32000
        return due - now
    }
}

struct PCMChunker {
    static let nominalBytes = 640 // 20 ms, 320 mono Int16 samples at 16 kHz.
    private(set) var partial = Data()
    mutating func append(_ bytes: Data) throws -> [Data] {
        guard bytes.count % 2 == 0 else { throw AudioFailure.invalidPCM }
        partial.append(bytes)
        var chunks: [Data] = []
        while partial.count >= Self.nominalBytes {
            chunks.append(Data(partial.prefix(Self.nominalBytes)))
            partial = Data(partial.dropFirst(Self.nominalBytes))
        }
        return chunks
    }
    mutating func finish() -> Data? {
        defer { partial.removeAll(keepingCapacity: false) }
        return partial.isEmpty ? nil : partial
    }
}

// A one-element wakeup channel carries no audio and creates no task per tap or
// notification. Actual work stays in separately bounded, locked storage.
final class AudioWakeup: @unchecked Sendable {
    let stream: AsyncStream<Void>
    private let continuation: AsyncStream<Void>.Continuation
    init() {
        let pair = AsyncStream<Void>.makeStream(bufferingPolicy: .bufferingNewest(1))
        stream = pair.stream; continuation = pair.continuation
    }
    func signal() { continuation.yield(()) }
    func finish() { continuation.finish() }
}

final class AudioEventMailbox: @unchecked Sendable {
    private let lock = NSLock()
    private var reason: AudioPauseReason?
    private var closed = false
    let wakeup = AudioWakeup()
    func offer(_ value: AudioPauseReason) {
        lock.lock()
        if !closed, reason == nil { reason = value }
        let notify = !closed
        lock.unlock()
        if notify { wakeup.signal() }
    }
    func take() -> AudioPauseReason? {
        lock.lock(); defer { lock.unlock() }
        let value = reason; reason = nil; return value
    }
    func close() {
        lock.lock(); closed = true; reason = nil; lock.unlock()
        wakeup.finish()
    }
}

import Foundation

struct CaptureTicket: Equatable, Sendable { let epoch: UUID; let connection: UInt64; let utterance: UUID }
struct PlaybackTicket: Equatable, Sendable { let epoch: UUID; let serial: UInt64; let generation: UInt32 }

struct PlaybackAdmission: Equatable, Sendable {
    let id: UUID
    let ticket: PlaybackTicket
    let connection: UInt32
    let sequence: UInt32
    let pcmBytes: Int
}

enum PlaybackOutcome: Equatable, Sendable { case consumed, cancelled, failed }

// Physical completion is distinct from cancellation of a Swift waiter. Native
// callbacks resolve once; cancelling wait() only wakes that waiter and MUST NOT
// claim the native buffer was consumed/cancelled. W09 retains its charge until
// real consumption or native flush acknowledgement plus waiter retirement.
final class PlaybackConsumption: @unchecked Sendable {
    private let lock = NSLock()
    private var outcome: PlaybackOutcome?
    private var waiter: CheckedContinuation<Void, Error>?
    private var claimed = false
    private var waiterCancelled = false
    var resolvedOutcome: PlaybackOutcome? {
        lock.lock(); defer { lock.unlock() }; return outcome
    }
    func resolve(_ value: PlaybackOutcome) {
        lock.lock()
        guard outcome == nil else { lock.unlock(); return }
        outcome = value; let pending = waiter; waiter = nil; lock.unlock()
        if let pending { Self.resume(pending, value) }
    }
    func wait() async throws {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                lock.lock()
                guard !claimed else { lock.unlock(); continuation.resume(throwing: VoiceFailure.invalidState); return }
                claimed = true
                if waiterCancelled { lock.unlock(); continuation.resume(throwing: CancellationError()) }
                else if let outcome { lock.unlock(); Self.resume(continuation, outcome) }
                else { waiter = continuation; lock.unlock() }
            }
            try Task.checkCancellation()
        } onCancel: { self.cancelWaiter() }
    }
    private func cancelWaiter() {
        lock.lock(); waiterCancelled = true; let pending = waiter; waiter = nil; lock.unlock()
        pending?.resume(throwing: CancellationError())
    }
    private static func resume(_ continuation: CheckedContinuation<Void, Error>, _ value: PlaybackOutcome) {
        switch value {
        case .consumed: continuation.resume()
        case .cancelled: continuation.resume(throwing: CancellationError())
        case .failed: continuation.resume(throwing: VoiceFailure.transport)
        }
    }
}

struct PlaybackReceipt: Sendable {
    let admission: PlaybackAdmission
    let consumption: PlaybackConsumption
}

// Pure bounded accounting used by the actor. Entries remain charged while
// queued, awaiting MainActor admission, natively scheduled or being cancelled.
struct PlaybackLedger: Sendable {
    struct Work: Sendable { let frame: AudioEnvelope; let admission: PlaybackAdmission }
    private enum Phase: Equatable, Sendable { case queued, admitting, scheduled }
    private struct Entry: Sendable {
        let work: Work
        var phase: Phase = .queued
        var nativeCancelled = false
        var consumerFinished = false
    }
    let epoch: UUID
    private(set) var serial: UInt64 = 0
    private(set) var bytes = 0
    var packets: Int { entries.count }
    private var entries: [UUID: Entry] = [:]
    private var queue: [UUID] = []
    init(epoch: UUID) { self.epoch = epoch }
    mutating func enqueue(_ frame: AudioEnvelope) throws {
        try frame.validate()
        guard frame.kind == .output else { throw VoiceFailure.invalidFrame }
        guard packets < 32, bytes + frame.pcm.count <= 9600 else { throw VoiceFailure.backpressure }
        let ticket = PlaybackTicket(epoch: epoch, serial: serial, generation: frame.generation)
        let admission = PlaybackAdmission(id: UUID(), ticket: ticket, connection: frame.connection,
            sequence: frame.sequence, pcmBytes: frame.pcm.count)
        entries[admission.id] = Entry(work: Work(frame: frame, admission: admission))
        queue.append(admission.id); bytes += frame.pcm.count
    }
    mutating func takeNext() -> Work? {
        guard !queue.isEmpty else { return nil }
        let id = queue.removeFirst()
        guard var entry = entries[id] else { return nil }
        entry.phase = .admitting; entries[id] = entry; return entry.work
    }
    // A receipt must describe the exact admission handed to the synchronous
    // native scheduler. A cancelled admission may return after flush: no waiter
    // needs starting, and the already acknowledged native cancellation retires it.
    mutating func scheduled(_ receipt: PlaybackReceipt, expected: PlaybackAdmission) throws -> Bool {
        guard receipt.admission == expected, var entry = entries[expected.id], entry.phase == .admitting else {
            throw VoiceFailure.invalidState
        }
        if entry.nativeCancelled { release(expected.id); return false }
        entry.phase = .scheduled; entries[expected.id] = entry; return true
    }
    // Keep uncertain scheduling charged until flush. Returns true only if this
    // failure still belongs to the current output serial and must end the stream.
    mutating func schedulingFailed(_ admission: PlaybackAdmission) throws -> Bool {
        guard var entry = entries[admission.id], entry.phase == .admitting else { throw VoiceFailure.invalidState }
        entry.consumerFinished = true; entries[admission.id] = entry
        if entry.nativeCancelled { release(admission.id) }
        return admission.ticket.serial == serial
    }
    mutating func finished(_ admission: PlaybackAdmission, outcome: PlaybackOutcome) throws {
        guard var entry = entries[admission.id], entry.work.admission == admission, entry.phase == .scheduled,
              !entry.consumerFinished else { throw VoiceFailure.invalidState }
        entry.consumerFinished = true; entries[admission.id] = entry
        if case .consumed = outcome { release(admission.id); return }
        if entry.nativeCancelled { release(admission.id); return }
        if admission.ticket.serial == serial { throw VoiceFailure.transport }
        // Cancellation/failure from an interrupted serial is not success; its
        // bytes stay charged until the native flush actually returns.
    }
    mutating func beginFlush() -> UInt64 {
        serial += 1
        // Unscheduled queue entries have no native owner; discard immediately.
        for id in queue { release(id) }; queue.removeAll()
        return serial
    }
    mutating func confirmFlush(nextSerial: UInt64) {
        for id in Array(entries.keys) {
            guard var entry = entries[id], entry.work.admission.ticket.serial < nextSerial else { continue }
            entry.nativeCancelled = true; entries[id] = entry
            if entry.consumerFinished { release(id) }
        }
    }
    // Lifecycle owner calls this ONLY after synchronous native flush/close AND
    // every scheduling/consumption child has joined. Restart cannot precede it.
    mutating func cleanupAfterJoin() { entries.removeAll(); queue.removeAll(); bytes = 0 }
    private mutating func release(_ id: UUID) {
        if let entry = entries.removeValue(forKey: id) { bytes -= entry.work.admission.pcmBytes }
    }
}

// W10 implements this port. All calls carry fences; an invalidated ticket MUST
// remain invalid even if an older start/play callback arrives after a flush.
// No AVFoundation, microphone activation or synthetic keep-alive lives in W09.
@MainActor protocol VoiceAudioPort: Sendable {
    func startCapture(ticket: CaptureTicket) throws
    func stopCapture(ticket: CaptureTicket)
    func muteCapture(_ muted: Bool, ticket: CaptureTicket)
    // ORDERED, SYNCHRONOUS MainActor native enqueue, never wait for consumption
    // here or hide scheduling in another Task. Validate every admission field.
    // Return only after native schedule succeeds, with a matching receipt. Only
    // actual played-back completion may resolve its consumption as .consumed.
    func scheduleOutput(_ frame: AudioEnvelope, admission: PlaybackAdmission) throws -> PlaybackReceipt
    // Invalidate serials below nextSerial, cancel scheduled buffers/continuations,
    // discard native queues and prevent late callbacks from restoring playback.
    // Before returning: native stop/unschedule is complete, and every older
    // receipt is resolved .cancelled (or was already physically consumed/failed).
    func flushOutput(epoch: UUID, nextSerial: UInt64)
    func close(epoch: UUID)
}

struct VoiceObservation: Sendable {
    let state: VoiceUIState
    let type: String
    let runID: String?
    let sessionID: String?
    let callID: String?
    let tool: String?
    let status: String?
    let durationMS: UInt64?
    let elapsedMS: UInt64?
    let reason: String?
    // Voice has no write operation ID or plan-approval capability.
}

@MainActor protocol VoiceObserver: Sendable {
    // Observation/transcript hooks are display-only, synchronous, and must not log
    // raw transcripts. UI retains at most 32 observations; IDs remain untrusted.
    func begin(epoch: UUID)
    func observe(_ observation: VoiceObservation, epoch: UUID)
    func replaceTranscript(id: String, value: WireState.Transcript, epoch: UUID)
    // Fence data callbacks immediately; final lifecycle observation is still allowed.
    func suspendData(epoch: UUID)
    func close(epoch: UUID)
}

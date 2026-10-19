import AVFoundation
import Foundation

// CampusPulse original. Lock protects converter, queue, partial and the one
// admitted delivery. @unchecked Sendable is confined to this synchronized seam.
final class CaptureMailbox: @unchecked Sendable {
    struct Delivery: Sendable {
        let id: UUID
        let ticket: CaptureTicket
        let revision: UUID
        let pcm: Data
    }
    private let lock = NSLock()
    let wakeup = AudioWakeup()
    private var ticket: CaptureTicket?
    private var revision = UUID()
    private var converter: CaptureConverter?
    private var chunker = PCMChunker()
    private var queue: [Data] = []
    private var inFlight: Delivery?
    private var failure: AudioFailure?
    private var accepting = false

    func prepare(ticket: CaptureTicket, revision: UUID, format: AVAudioFormat) throws {
        lock.lock(); defer { lock.unlock() }
        guard failure == nil else { throw failure! }
        self.ticket = ticket; self.revision = revision
        queue.removeAll(); chunker = PCMChunker()
        converter = try CaptureConverter(format: format); accepting = true
    }
    private var retainedBytes: Int {
        queue.reduce(0) { $0 + $1.count } + chunker.partial.count + (inFlight?.pcm.count ?? 0)
    }
    private func append(_ bytes: Data, final: Bool = false) throws {
        guard retainedBytes + bytes.count <= 6400 else { throw AudioFailure.capacity }
        queue += try chunker.append(bytes)
        if final, let tail = chunker.finish() { queue.append(tail) }
        guard queue.count + (inFlight == nil ? 0 : 1) <= 16 else { throw AudioFailure.capacity }
    }
    func ingest(_ buffer: AVAudioPCMBuffer, ticket: CaptureTicket, revision: UUID) {
        lock.lock()
        guard accepting, self.ticket == ticket, self.revision == revision, let converter else { lock.unlock(); return }
        do { try append(converter.convert(buffer)) }
        catch { failLocked((error as? AudioFailure) ?? .conversion) }
        lock.unlock(); wakeup.signal()
    }
    // Explicit end drains converter and the final 2...638-byte partial before
    // VoiceSession.endUtterance closes admission. Stop/mute instead discard it.
    func finish(ticket: CaptureTicket, revision: UUID) throws {
        lock.lock(); defer { lock.unlock(); wakeup.signal() }
        guard self.ticket == ticket, self.revision == revision, failure == nil, let converter else {
            throw AudioFailure.retiredTicket
        }
        accepting = false
        do { try append(converter.convert(nil, end: true), final: true); self.converter = nil }
        catch { failLocked((error as? AudioFailure) ?? .conversion); throw error }
    }
    private func failLocked(_ error: AudioFailure) {
        failure = error; accepting = false; converter = nil
        queue.removeAll(); chunker = PCMChunker()
    }
    func error() -> AudioFailure? { lock.lock(); defer { lock.unlock() }; return failure }
    func take() -> Delivery? {
        lock.lock(); defer { lock.unlock() }
        guard failure == nil, inFlight == nil, !queue.isEmpty, let ticket else { return nil }
        let value = Delivery(id: UUID(), ticket: ticket, revision: revision, pcm: queue.removeFirst())
        inFlight = value; return value
    }
    func acknowledge(_ id: UUID) {
        lock.lock(); if inFlight?.id == id { inFlight = nil }; lock.unlock(); wakeup.signal()
    }
    func discard() {
        lock.lock(); accepting = false; ticket = nil; revision = UUID(); converter = nil
        queue.removeAll(); chunker = PCMChunker(); lock.unlock()
        // inFlight remains charged until its owner acknowledges it.
        wakeup.signal()
    }
    var drained: Bool {
        lock.lock(); defer { lock.unlock() }
        return queue.isEmpty && inFlight == nil && chunker.partial.isEmpty
    }
    var bytesRetained: Int { lock.lock(); defer { lock.unlock() }; return retainedBytes }
    func close() { discard(); wakeup.finish() }
}

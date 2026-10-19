

















import AVFoundation
import Foundation

@MainActor final class Microphone {
    private let engine: AVAudioEngine
    private var installed = false
    init(engine: AVAudioEngine) { self.engine = engine }
    func start(ticket: CaptureTicket, revision: UUID, mailbox: CaptureMailbox) throws {
        guard !installed else { throw AudioFailure.inactive }
        let node = engine.inputNode
        let format = node.outputFormat(forBus: 0)
        try mailbox.prepare(ticket: ticket, revision: revision, format: format)
        let requested = AVAudioFrameCount(max(1, Int((format.sampleRate * 0.020).rounded())))


        node.installTap(onBus: 0, bufferSize: requested, format: format) { buffer, _ in
            mailbox.ingest(buffer, ticket: ticket, revision: revision)
        }
        installed = true
        do { if !engine.isRunning { try engine.start() } }
        catch { mailbox.discard(); stop(); throw AudioFailure.graph }
    }
    func stop() {
        if installed { installed = false; engine.inputNode.removeTap(onBus: 0) }
    }
}

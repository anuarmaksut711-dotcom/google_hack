

















import AVFoundation
import Foundation
import UIKit

@MainActor protocol AudioSessionDriving: AnyObject {
    var canStart: Bool { get }
    func requestPermission() async -> Bool
    func activate() throws
    func deactivate()
}

@MainActor protocol AudioGraph: AnyObject {
    var player: AudioPlayer { get }
    func startCapture(ticket: CaptureTicket, revision: UUID, mailbox: CaptureMailbox) throws
    func stopCapture()
    func shutdown()
}

@MainActor final class AudioController: VoiceAudioPort {
    typealias GraphFactory = @MainActor (AudioEventMailbox) throws -> any AudioGraph
    private let driver: any AudioSessionDriving
    private let graphFactory: GraphFactory


    private let beforeArming: @Sendable (CaptureReady) async throws -> Void
    private weak var voice: VoiceSession?
    private var fences = AudioFences()
    private var graph: (any AudioGraph)?
    private var mailbox: CaptureMailbox?
    private var events: AudioEventMailbox?
    private var pump: Task<Void, Never>?
    private var watcher: Task<Void, Never>?
    private var lifetime = UUID()
    private var starting: UUID?
    private var revision = UUID()
    private var needsArm = false
    private var muted = false
    private var finishing = false
    private var pacer = CapturePacer()
    private var arming: CaptureArming?
    private var muteRequest: UUID?
    private(set) var captureStatus: CaptureStatus = .inactive
    var onCaptureStatus: (@MainActor (CaptureStatus) -> Void)?
    private(set) var pauseReason: AudioPauseReason?
    var onPause: (@MainActor (AudioPauseReason) -> Void)?

    init(driver: any AudioSessionDriving,
         beforeArming: @escaping @Sendable (CaptureReady) async throws -> Void = { _ in },
         graphFactory: @escaping GraphFactory) {
        self.driver = driver; self.graphFactory = graphFactory; self.beforeArming = beforeArming
    }
    static func native() -> AudioController {
        AudioController(driver: NativeAudioSession()) { try NativeAudioGraph(events: $0) }
    }


    func bind(_ session: VoiceSession) throws {
        guard graph == nil, pump == nil, watcher == nil, starting == nil else { throw AudioFailure.inactive }
        voice = session
    }



    func userStart() async throws {
        guard voice != nil, driver.canStart, starting == nil, graph == nil,
              pump == nil, watcher == nil else { throw AudioFailure.inactive }
        let attempt = UUID(), token = lifetime
        starting = attempt
        let allowed = await driver.requestPermission()
        guard starting == attempt else { throw AudioFailure.inactive }
        starting = nil
        guard token == lifetime, !Task.isCancelled, driver.canStart, voice != nil else { throw AudioFailure.inactive }
        guard allowed else { throw AudioFailure.permissionDenied }
        do {
            try driver.activate()
            try fences.authorize()
            let box = CaptureMailbox(), signals = AudioEventMailbox()
            mailbox = box; events = signals
            graph = try graphFactory(signals)
            pacer = CapturePacer()
            pauseReason = nil
            pump = Task { @MainActor [weak self] in
                for await _ in box.wakeup.stream {
                    guard let self, !Task.isCancelled, self.lifetime == token else { return }
                    await self.pumpCapture(box, token: token)
                }
            }
            watcher = Task { @MainActor [weak self] in
                for await _ in signals.wakeup.stream {
                    guard let self, !Task.isCancelled, self.lifetime == token else { return }
                    if let reason = signals.take() { await self.pauseAndStop(reason); return }
                }
            }
        } catch {
            stopHardware(); throw ApplicationError((error as? AudioFailure) ?? .graph)
        }
    }
    func startCapture(ticket: CaptureTicket) throws {
        guard graph != nil, let mailbox else { throw AudioFailure.inactive }
        let already = fences.capture == ticket
        try fences.begin(ticket)
        if already { return }
        revision = UUID(); needsArm = true; muted = false; finishing = false
        arming?.resolve(.failure(.retiredTicket))
        let ack = CaptureArming(CaptureReady(ticket: ticket, lifetime: lifetime, revision: revision))
        arming = ack; setCaptureStatus(.arming(ack.identity))
        mailbox.wakeup.signal()
    }
    func stopCapture(ticket: CaptureTicket) {
        let current = fences.capture == ticket
        fences.retire(ticket)
        if graph != nil, !fences.authorized { stopHardware(); return }
        guard current else { return }
        arming?.resolve(.failure(.retiredTicket)); arming = nil
        revision = UUID(); needsArm = false; finishing = false
        mailbox?.discard(); graph?.stopCapture()
        setCaptureStatus(.inactive)
    }
    func muteCapture(_ value: Bool, ticket: CaptureTicket) {
        guard fences.authorized, fences.capture == ticket, muted != value else { return }
        arming?.resolve(.failure(.armingCancelled)); arming = nil
        muted = value; revision = UUID(); needsArm = !value; finishing = false
        mailbox?.discard(); graph?.stopCapture()
        if value { setCaptureStatus(.muted(ticket)) }
        else {
            let ack = CaptureArming(CaptureReady(ticket: ticket, lifetime: lifetime, revision: revision))
            arming = ack; setCaptureStatus(.arming(ack.identity)); mailbox?.wakeup.signal()
        }
    }
    func scheduleOutput(_ frame: AudioEnvelope, admission: PlaybackAdmission) throws -> PlaybackReceipt {
        try fences.accept(admission.ticket)
        guard let graph else { throw AudioFailure.inactive }
        do { return try graph.player.scheduleOutput(frame, admission: admission) }
        catch {
            events?.offer(.playbackFailure)
            throw error
        }
    }
    func flushOutput(epoch: UUID, nextSerial: UInt64) {
        if fences.flush(epoch: epoch, nextSerial: nextSerial) {
            graph?.player.flush(epoch: epoch, nextSerial: nextSerial)
        }
    }
    func close(epoch: UUID) {
        if fences.close(epoch) { stopHardware() }
    }




    func userEndUtterance() async throws {
        guard let ticket = fences.capture, fences.authorized, !muted, !finishing,
              !needsArm, let ack = arming, isCurrent(ack.identity),
              let mailbox, let voice else { throw AudioFailure.inactive }
        let token = lifetime, captureRevision = revision
        finishing = true
        setCaptureStatus(.ending(ack.identity))
        do {
            guard valid(ticket, revision: captureRevision, token: token) else { throw AudioFailure.retiredTicket }
            graph?.stopCapture()
            try mailbox.finish(ticket: ticket, revision: captureRevision)
            for _ in 0..<100 {
                guard valid(ticket, revision: captureRevision, token: token) else { throw AudioFailure.retiredTicket }
                if let error = mailbox.error() { throw error }
                if mailbox.drained {
                    try await voice.endUtterance()
                    try await waitWire(voice, token: token) { !$0.inputOpen }
                    return
                }
                try await Task.sleep(nanoseconds: 5_000_000)
            }
            throw AudioFailure.drainTimeout
        } catch {
            if valid(ticket, revision: captureRevision, token: token) { await pauseAndStop(.captureFailure) }
            throw error
        }
    }

    func userBeginUtterance() async throws -> CaptureReady {
        guard fences.authorized, let voice else { throw AudioFailure.inactive }
        let token = lifetime
        let ticket = try await voice.beginUtterance()
        guard lifetime == token, let ack = arming, ack.identity.ticket == ticket else { throw AudioFailure.retiredTicket }
        return try await awaitArming(ack)
    }


    @discardableResult func userMute(_ value: Bool) async throws -> CaptureReady? {
        guard fences.authorized, muteRequest == nil, let voice else { throw AudioFailure.inactive }
        let token = lifetime, request = UUID()
        muteRequest = request
        defer { if muteRequest == request { muteRequest = nil } }
        do {
            let current = await voice.machine
            guard lifetime == token, !current.terminal, let wire = current.wire,
                  wire.phase == .active else { throw AudioFailure.retiredTicket }


            if wire.muted == value, (fences.capture == nil || muted == value) {
                if !value, let ack = arming { return try await awaitArming(ack) }
                return nil
            }
            try await voice.setMuted(value)
            guard lifetime == token else { throw AudioFailure.retiredTicket }
            if value {


                try await waitWire(voice, token: token) { $0.muted }
                return nil
            }
            guard fences.capture != nil, let ack = arming else {
                try await waitWire(voice, token: token) { !$0.muted }
                return nil
            }
            return try await awaitArming(ack)
        } catch {
            if token == lifetime, fences.authorized { await pauseAndStop(.captureFailure) }
            throw error
        }
    }
    func isCurrent(_ ready: CaptureReady) -> Bool {
        valid(ready.ticket, revision: ready.revision, token: ready.lifetime)
            && !needsArm && captureStatus == .armed(ready)
    }
    private func setCaptureStatus(_ value: CaptureStatus) {
        captureStatus = value; onCaptureStatus?(value)
    }
    private func waitWire(_ voice: VoiceSession, token: UUID,
                          condition: @escaping @Sendable (WireState) -> Bool) async throws {
        do {
            try await bounded(seconds: 0.250) {
                while true {
                    try Task.checkCancellation()
                    let machine = await voice.machine
                    guard !machine.terminal, let wire = machine.wire, wire.phase == .active else {
                        throw AudioFailure.retiredTicket
                    }
                    if condition(wire) { return }
                    try await Task.sleep(nanoseconds: 5_000_000)
                }
            }
        } catch is CancellationError { throw AudioFailure.armingCancelled }
        catch let error as AudioFailure { throw error }
        catch { throw AudioFailure.admissionTimeout }
        guard lifetime == token, fences.authorized else { throw AudioFailure.retiredTicket }
    }
    private func awaitArming(_ ack: CaptureArming) async throws -> CaptureReady {
        do {
            let ready: CaptureReady
            if let result = ack.resolved { ready = try result.get() }
            else { ready = try await bounded(seconds: 0.300) { try await ack.wait() } }
            try Task.checkCancellation()
            guard isCurrent(ready) else { throw AudioFailure.retiredTicket }
            return ready
        } catch {
            let failure: AudioFailure
            if (error as? VoiceFailure) == .timeout { failure = .admissionTimeout }
            else { failure = (error as? AudioFailure) ?? (error is CancellationError ? .armingCancelled : .graph) }
            ack.resolve(.failure(failure))
            if arming === ack, valid(ack.identity.ticket, revision: ack.identity.revision, token: ack.identity.lifetime) {
                setCaptureStatus(.failed(failure)); await pauseAndStop(.captureFailure)
            }
            throw failure
        }
    }
    func userStop() async {
        let oldPump = pump, oldWatcher = watcher
        stopHardware()
        let stoppedLifetime = lifetime
        await voice?.stop()
        await oldPump?.value; await oldWatcher?.value
        if lifetime == stoppedLifetime { pump = nil; watcher = nil }
    }
    private func valid(_ ticket: CaptureTicket, revision value: UUID, token: UUID) -> Bool {
        fences.authorized && fences.capture == ticket && revision == value && lifetime == token && !muted
    }
    private func pumpCapture(_ box: CaptureMailbox, token: UUID) async {
        if let error = box.error() {
            await pauseAndStop(error == .capacity ? .overflow : .captureFailure); return
        }
        if needsArm, let ticket = fences.capture, let voice, let ack = arming {
            let value = revision
            do {



                let gate = beforeArming, identity = ack.identity
                try await bounded(seconds: 0.250) {
                    try await gate(identity)
                    while true {
                        try Task.checkCancellation()
                        let machine = await voice.machine
                        guard !machine.terminal, machine.state != .stopping else { throw AudioFailure.retiredTicket }
                        if let wire = machine.wire, wire.connection == ticket.connection,
                           wire.phase == .active, wire.inputOpen, !wire.muted { return }
                        try await Task.sleep(nanoseconds: 5_000_000)
                    }
                }
                guard valid(ticket, revision: value, token: token), arming === ack,
                      ack.resolved == nil, !Task.isCancelled else { return }
                guard let graph else { throw AudioFailure.inactive }
                try graph.startCapture(ticket: ticket, revision: value, mailbox: box)
                guard valid(ticket, revision: value, token: token), arming === ack,
                      ack.resolved == nil, !Task.isCancelled else {
                    box.discard()
                    if token != lifetime { graph.shutdown() } else { graph.stopCapture() }
                    return
                }
                needsArm = false
                ack.resolve(.success(identity)); setCaptureStatus(.armed(identity))
            } catch {
                let failure = (error as? AudioFailure) ?? (error is CancellationError ? .armingCancelled : .admissionTimeout)
                ack.resolve(.failure(failure))
                if arming === ack { setCaptureStatus(.failed(failure)) }
                if valid(ticket, revision: value, token: token) { await pauseAndStop(.captureFailure) }
                return
            }
        }
        while !Task.isCancelled, token == lifetime, let delivery = box.take() {
            do {
                if valid(delivery.ticket, revision: delivery.revision, token: token), let voice {



                    let delay = try pacer.reserve(bytes: delivery.pcm.count, now: ProcessInfo.processInfo.systemUptime)
                    if delay > 0 { try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) }
                    if valid(delivery.ticket, revision: delivery.revision, token: token), !Task.isCancelled {
                        try await voice.submitPCM(delivery.pcm, ticket: delivery.ticket)
                    }
                }
                box.acknowledge(delivery.id)
            } catch {
                box.acknowledge(delivery.id)
                if valid(delivery.ticket, revision: delivery.revision, token: token) {
                    await pauseAndStop(.captureFailure)
                }
                return
            }
        }
    }
    private func pauseAndStop(_ reason: AudioPauseReason) async {
        guard graph != nil else { return }
        pauseReason = reason; stopHardware(); onPause?(reason)
        await voice?.stop()

    }
    private func stopHardware() {
        arming?.resolve(.failure(.retiredTicket)); arming = nil
        muteRequest = nil
        lifetime = UUID(); fences.stop(); revision = UUID()
        needsArm = false; finishing = false; muted = false
        mailbox?.close(); events?.close()
        pump?.cancel(); watcher?.cancel()
        graph?.shutdown(); graph = nil; mailbox = nil; events = nil
        driver.deactivate()
        setCaptureStatus(.inactive)
    }
}

@MainActor final class NativeAudioSession: AudioSessionDriving {
    private var activated = false
    var canStart: Bool { UIApplication.shared.applicationState == .active }
    func requestPermission() async -> Bool {


        #if compiler(>=5.9)
        if #available(iOS 17.0, *) {
            return await withCheckedContinuation { continuation in
                AVAudioApplication.requestRecordPermission { continuation.resume(returning: $0) }
            }
        }
        #endif
        return await withCheckedContinuation { continuation in
            AVAudioSession.sharedInstance().requestRecordPermission { continuation.resume(returning: $0) }
        }
    }
    func activate() throws {
        let session = AVAudioSession.sharedInstance()



        try session.setCategory(.playAndRecord, mode: .voiceChat, options: [.allowBluetooth])
        try session.setPreferredIOBufferDuration(0.020)
        try session.setActive(true)
        activated = true
    }
    func deactivate() {
        guard activated else { return }; activated = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}

@MainActor final class NativeAudioGraph: AudioGraph {
    let player: AudioPlayer
    private let engine: AVAudioEngine
    private let microphone: Microphone
    private var observers: [NSObjectProtocol] = []
    private var closed = false
    init(events: AudioEventMailbox) throws {
        let engine = AVAudioEngine()
        let session = AVAudioSession.sharedInstance()
        let privatePorts: [AVAudioSession.Port] = [.headphones, .bluetoothA2DP, .bluetoothLE, .bluetoothHFP]
        let headphones = session.currentRoute.outputs.contains { privatePorts.contains($0.portType) }
        if #available(iOS 13.0, *) { try engine.inputNode.setVoiceProcessingEnabled(!headphones) }
        else { throw AudioFailure.graph }
        let signature = Self.routeSignature()
        let scheduler = try NativePlaybackScheduler(engine: engine) { Self.routeSignature() == signature }
        self.engine = engine; microphone = Microphone(engine: engine)
        player = AudioPlayer(scheduler: scheduler, onFailure: { events.offer(.playbackFailure) })
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: AVAudioSession.interruptionNotification, object: session, queue: nil) { note in
            let kind = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? NSNumber)?.uintValue
            if kind == AVAudioSession.InterruptionType.began.rawValue { events.offer(.interruption) }

        })
        observers.append(center.addObserver(forName: AVAudioSession.routeChangeNotification, object: session, queue: nil) { note in
            let reason = (note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? NSNumber)?.uintValue
            events.offer(reason == AVAudioSession.RouteChangeReason.oldDeviceUnavailable.rawValue ? .headphonesRemoved : .routeChanged)
        })
        observers.append(center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: session, queue: nil) { _ in events.offer(.mediaReset) })
        observers.append(center.addObserver(forName: AVAudioSession.mediaServicesWereLostNotification, object: session, queue: nil) { _ in events.offer(.mediaLost) })
        observers.append(center.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { _ in events.offer(.engineChanged) })
    }
    private static func routeSignature() -> String {
        let session = AVAudioSession.sharedInstance()
        return (session.currentRoute.inputs + session.currentRoute.outputs).map { $0.uid + ":" + $0.portType.rawValue }.joined(separator: "|")
            + ":" + String(session.sampleRate)
    }
    func startCapture(ticket: CaptureTicket, revision: UUID, mailbox: CaptureMailbox) throws {
        guard !closed else { throw AudioFailure.inactive }
        try microphone.start(ticket: ticket, revision: revision, mailbox: mailbox)
        guard !closed else { engine.stop(); microphone.stop(); throw AudioFailure.retiredTicket }
    }
    func stopCapture() { microphone.stop() }
    func shutdown() {
        guard !closed else { engine.stop(); microphone.stop(); return }; closed = true
        observers.forEach { NotificationCenter.default.removeObserver($0) }; observers.removeAll()


        player.shutdown { self.engine.stop(); self.microphone.stop() }
        if #available(iOS 13.0, *), engine.inputNode.isVoiceProcessingEnabled {
            try? engine.inputNode.setVoiceProcessingEnabled(false)
        }
        engine.reset()
    }
}

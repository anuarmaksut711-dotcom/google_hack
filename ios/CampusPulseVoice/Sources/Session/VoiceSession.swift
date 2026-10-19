import Foundation

actor VoiceSession {
    let claim: VoiceClaim
    private enum Outbound: Sendable {
        case start(Data), control(Data), pcm(Data), stop(Data)
        var bytes: Int {
            switch self { case .start(let d), .control(let d), .pcm(let d), .stop(let d): return d.count }
        }
    }
    private let audio: any VoiceAudioPort
    private let observer: any VoiceObserver
    private let now: @Sendable () -> Double
    private(set) var machine = SessionMachine()
    private var socket: (any VoiceSocket)?
    private var owner: Task<Void, Never>?
    private var epoch = UUID()
    private var requestID: UInt64 = 0
    private var credentialDeadline = 0.0
    private var nextHeartbeat = 0.0
    private var outgoing: [Outbound] = []
    private var inputBytes = 0
    private var controlBytes = 0
    private var controlCount = 0
    private var outgoingSerial: UInt64 = 0
    private var admittedWrite = false
    private var playback = PlaybackLedger(epoch: UUID())
    private var captureTicket: CaptureTicket?
    private var captureEnabled = false
    private var inputPaused = false
    private var uncertain = false
    private var runID: String?
    private var sessionID: String?
    private var writeSignal: AsyncStream<Void>.Continuation?
    private var playSignal: AsyncStream<Void>.Continuation?

    init(claim: VoiceClaim, audio: any VoiceAudioPort, observer: any VoiceObserver,
         now: @escaping @Sendable () -> Double = { ProcessInfo.processInfo.systemUptime }) {
        self.claim = claim; self.audio = audio; self.observer = observer; self.now = now
    }
    var state: VoiceUIState { machine.state }
    var playbackUsage: (bytes: Int, packets: Int) { (playback.bytes, playback.packets) }

    func startNative(credential: DeviceCredential, reconnect: Bool = false) async throws {
        guard owner == nil, VoiceClaim(origin: claim.origin, credential: credential) == claim else { throw VoiceFailure.invalidState }
        let socket = try NativeVoiceSocket(origin: claim.origin, credential: credential)
        do { try start(socket: socket, reconnect: reconnect) }
        catch { await socket.close(); throw VoiceFailure.invalidState }
    }



    func start(socket: any VoiceSocket, wallNow: Date = Date(), reconnect: Bool = false) throws {
        guard owner == nil, socket.claim == claim, claim.expiresAt > wallNow else { throw VoiceFailure.invalidState }
        if reconnect { machine.resetForReconnect() }
        let start = try machine.begin(now: now(), reconnect: reconnect)
        epoch = UUID(); let ticket = epoch
        outgoingSerial += 1
        self.socket = socket; requestID = 0; uncertain = false
        admittedWrite = false
        credentialDeadline = now() + min(540, claim.expiresAt.timeIntervalSince(wallNow)); nextHeartbeat = now() + 10
        outgoing = [.start(start)]; controlBytes = start.count; controlCount = 1; inputBytes = 0
        playback = PlaybackLedger(epoch: ticket); captureEnabled = false; captureTicket = nil; inputPaused = false
        runID = nil; sessionID = nil
        let writes = AsyncStream<Void>.makeStream(bufferingPolicy: .bufferingNewest(1))
        let plays = AsyncStream<Void>.makeStream(bufferingPolicy: .bufferingNewest(1))
        writeSignal = writes.continuation; playSignal = plays.continuation
        writeSignal?.yield(())
        owner = Task { await self.drive(socket: socket, epoch: ticket, writes: writes.stream, plays: plays.stream) }
    }
    private func id() -> String { requestID += 1; return "request_" + String(requestID) }
    private func active() throws -> UInt64 {
        guard !uncertain, !machine.terminal, machine.state != .stopping, let wire = machine.wire, wire.phase == .active else { throw VoiceFailure.invalidState }
        return wire.connection
    }
    private func enqueue(_ item: Outbound) throws {
        guard outgoing.count + (admittedWrite ? 1 : 0) < 32 else { throw VoiceFailure.backpressure }
        switch item {
        case .pcm:
            guard inputBytes + item.bytes <= 6400 else { throw VoiceFailure.backpressure }; inputBytes += item.bytes
        default:
            guard controlCount < 32, controlBytes + item.bytes <= 65536 else { throw VoiceFailure.backpressure }
            controlCount += 1; controlBytes += item.bytes
        }
        outgoing.append(item); writeSignal?.yield(())
    }



    func beginUtterance() async throws -> CaptureTicket {
        let connection = try active()
        guard !captureEnabled, !inputPaused, machine.wire?.muted == false else { throw VoiceFailure.invalidState }
        if machine.wire?.inputOpen == false {
            try enqueue(.control(WireJSON.control("input", request: id(), connection: connection, action: "begin")))
        }
        let ticket = CaptureTicket(epoch: epoch, connection: connection, utterance: UUID())
        captureEnabled = true; captureTicket = ticket
        do { try await audio.startCapture(ticket: ticket) }
        catch {
            if captureTicket == ticket, ticket.epoch == epoch { await abortConnection() }
            await audio.stopCapture(ticket: ticket)
            throw VoiceFailure.transport
        }
        guard captureEnabled, captureTicket == ticket, !machine.terminal, machine.state != .stopping else {
            await audio.stopCapture(ticket: ticket); throw VoiceFailure.invalidState
        }
        return ticket
    }
    func submitPCM(_ pcm: Data, ticket: CaptureTicket) async throws {
        _ = try active()
        guard captureEnabled, !inputPaused, ticket == captureTicket, ticket.epoch == epoch,
              machine.wire?.muted == false else { throw VoiceFailure.invalidState }
        guard pcm.count >= 2, pcm.count <= 1280, pcm.count % 2 == 0 else { throw VoiceFailure.invalidLength }
        do { try enqueue(.pcm(pcm)) }
        catch { await abortConnection(); throw VoiceFailure.backpressure }
    }
    func endUtterance() async throws {
        let connection = try active()
        guard captureEnabled, let ticket = captureTicket else { throw VoiceFailure.invalidState }
        captureEnabled = false; inputPaused = true


        await audio.stopCapture(ticket: ticket)
        guard ticket.epoch == epoch, !machine.terminal, machine.state != .stopping else { return }
        do { try enqueue(.control(WireJSON.control("input", request: id(), connection: connection, action: "end"))) }
        catch { await abortConnection(); throw VoiceFailure.backpressure }
    }
    func setMuted(_ value: Bool) async throws {
        let connection = try active()
        let ticket = epoch
        inputPaused = true
        if let ticket = captureTicket { await audio.muteCapture(value, ticket: ticket) }
        guard ticket == epoch, !machine.terminal, machine.state != .stopping else { return }
        do { try enqueue(.control(WireJSON.control("mute", request: id(), connection: connection, muted: value))) }
        catch { await abortConnection(); throw VoiceFailure.backpressure }
    }


    func stop() async {
        let running = owner
        await requestStop()
        await running?.value
    }
    private func abortConnection() async {
        let ticket = epoch, capture = captureTicket, transport = socket
        captureEnabled = false; inputPaused = true; uncertain = true; machine.fail()
        await observer.suspendData(epoch: ticket)
        await transport?.close()
        guard ticket == epoch else { return }
        await flushOutput()
        if let capture { await audio.stopCapture(ticket: capture) }
    }
    private func requestStop() async {
        guard owner != nil, machine.state != .stopping, !machine.terminal else { return }
        let ticket = epoch, capture = captureTicket, transport = socket
        captureEnabled = false; inputPaused = true
        do {
            let stop = try machine.stop(now: now(), request: id())
            outgoingSerial += 1
            outgoing.removeAll(); inputBytes = 0; controlBytes = 0; controlCount = 0
            if let stop { try enqueue(.stop(stop)) }
            else { await transport?.close() }
        } catch { machine.fail(); uncertain = true; await transport?.close() }
        await observer.suspendData(epoch: ticket)
        guard ticket == epoch else { return }
        await flushOutput()
        if let capture { await audio.stopCapture(ticket: capture) }
    }
    private func flushOutput() async {
        let ticket = epoch, nextSerial = playback.beginFlush()
        await audio.flushOutput(epoch: ticket, nextSerial: nextSerial)
        guard ticket == epoch else { return }


        playback.confirmFlush(nextSerial: nextSerial)
    }
    private func drive(socket: any VoiceSocket, epoch ticket: UUID, writes: AsyncStream<Void>, plays: AsyncStream<Void>) async {
        await observer.begin(epoch: ticket)
        do {
            try await withThrowingTaskGroup(of: Void.self) { group in
                group.addTask { try await self.readLoop(socket, epoch: ticket) }
                group.addTask { try await self.writeLoop(socket, epoch: ticket, signals: writes) }
                group.addTask { try await self.playLoop(epoch: ticket, signals: plays) }
                group.addTask { try await self.watchLoop(epoch: ticket) }
                do { _ = try await group.next() }
                catch {
                    if !(machine.state == .stopping && machine.terminal) { machine.fail() }
                }


                captureEnabled = false; inputPaused = true; uncertain = true
                group.cancelAll(); writeSignal?.finish(); playSignal?.finish()
                await socket.close(); await flushOutput(); await audio.close(epoch: ticket)

            }
        } catch { machine.fail() }
        guard ticket == epoch else { return }
        machine.cleanupCompleted(failed: machine.state == .error)
        outgoing.removeAll(); playback.cleanupAfterJoin(); inputBytes = 0; controlBytes = 0; controlCount = 0; admittedWrite = false
        captureTicket = nil; socketCleanup()
        await emit(type: machine.state == .error ? "error" : "closed", message: nil)
        await observer.close(epoch: ticket)
        owner = nil
    }
    private func socketCleanup() { socket = nil; writeSignal = nil; playSignal = nil }
    private func readLoop(_ socket: any VoiceSocket, epoch ticket: UUID) async throws {
        while !Task.isCancelled {
            let packet = try await socket.receive()
            guard ticket == epoch else { return }
            let effect = try machine.receive(packet, now: now())
            switch effect {
            case .closed: return
            case .discard: break
            case .stopCommand: await requestStop()
            case .audio(let frame):
                try playback.enqueue(frame); playSignal?.yield(())
            case .event(let m):
                if m.type == "ready" { runID = try m.text("run_id"); sessionID = try m.text("session_id") }
                if m.type == "interrupted" { await flushOutput() }
                guard ticket == epoch, !machine.terminal, machine.state != .stopping else { continue }
                if m.type == "transcript", let value = machine.wire?.transcripts[try m.text("transcript_id")] {
                    await observer.replaceTranscript(id: try m.text("transcript_id"), value: value, epoch: ticket)
                } else { await emit(type: m.type, message: m) }
            }
        }
    }
    private func writeLoop(_ socket: any VoiceSocket, epoch ticket: UUID, signals: AsyncStream<Void>) async throws {
        for await _ in signals {
            while !outgoing.isEmpty {
                try Task.checkCancellation()
                guard ticket == epoch, !uncertain else { return }
                let item = outgoing.removeFirst()
                admittedWrite = true
                let serial = outgoingSerial
                let packet: VoicePacket
                switch item {
                case .start(let d), .stop(let d):
                    packet = .text(d)
                case .control(let d):
                    try machine.admit(d, now: now()); packet = .text(d)
                    let m = try WireJSON.parse(d, client: true)
                    if m.type == "input" { inputPaused = false }
                    if m.type == "mute" { inputPaused = false }
                case .pcm(let d):

                    packet = .binary(try machine.admitPCM(d, now: now()).encode())
                }
                do { try await bounded(seconds: 2) { try await socket.send(packet) } }
                catch {
                    uncertain = true; machine.fail(); await socket.close(); throw VoiceFailure.transport
                }
                admittedWrite = false
                if serial == outgoingSerial {
                    switch item {
                    case .pcm(let d): inputBytes -= d.count
                    default: controlCount -= 1; controlBytes -= item.bytes
                    }
                }
            }
        }
    }
    private enum PlaybackEvent: Sendable {
        case wake
        case finished(PlaybackAdmission, PlaybackOutcome)
    }
    private func playLoop(epoch ticket: UUID, signals: AsyncStream<Void>) async throws {
        try await withThrowingTaskGroup(of: PlaybackEvent.self) { group in
            defer { group.cancelAll() }




            group.addTask {
                for await _ in signals { return .wake }
                throw CancellationError()
            }
            while let event = try await group.next() {
                try Task.checkCancellation()
                guard ticket == epoch else { return }
                switch event {
                case .wake:
                    while !machine.terminal, machine.state != .stopping, let work = playback.takeNext() {
                        try Task.checkCancellation()
                        let receipt: PlaybackReceipt
                        do {
                            receipt = try await audio.scheduleOutput(work.frame, admission: work.admission)
                            guard try playback.scheduled(receipt, expected: work.admission) else { continue }
                        } catch {


                            if try playback.schedulingFailed(work.admission) { throw VoiceFailure.transport }
                            continue
                        }
                        group.addTask {
                            let outcome: PlaybackOutcome
                            do {
                                try await bounded(seconds: 2) { try await receipt.consumption.wait() }
                                outcome = .consumed
                            } catch is CancellationError { outcome = .cancelled }
                            catch { outcome = .failed }
                            return .finished(work.admission, outcome)
                        }
                    }
                    group.addTask {
                        for await _ in signals { return .wake }
                        throw CancellationError()
                    }
                case .finished(let admission, let outcome):
                    try playback.finished(admission, outcome: outcome)
                }
            }
        }
    }
    private func watchLoop(epoch ticket: UUID) async throws {
        while !Task.isCancelled {
            try await Task.sleep(nanoseconds: 50_000_000)
            guard ticket == epoch else { return }
            try machine.checkTime(now: now())
            if now() >= credentialDeadline { throw VoiceFailure.timeout }
            if machine.terminal { return }
            if machine.state != .stopping, machine.wire?.phase == .active, now() >= nextHeartbeat {
                try enqueue(.control(WireJSON.control("heartbeat", request: id(), connection: machine.wire!.connection)))
                nextHeartbeat = now() + 10
            }
        }
    }
    private func emit(type: String, message: WireMessage?) async {
        let value = VoiceObservation(state: machine.state, type: type, runID: runID, sessionID: sessionID,
            callID: try? message?.text("call_id"), tool: try? message?.text("tool"), status: try? message?.text("status"),
            durationMS: try? message?.uint("duration_ms"), elapsedMS: try? message?.uint("elapsed_ms"),
            reason: (try? message?.text("reason")) ?? machine.closedReason)
        await observer.observe(value, epoch: epoch)
    }
}

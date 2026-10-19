import Combine
import Foundation
import UIKit

@MainActor final class VoiceScreenModel: ObservableObject {
    typealias ConversationFactory = @MainActor (VoiceClaim, AppObserver) throws -> any AppConversation
    typealias PairingFactory = @MainActor (BackendOrigin) -> DevicePairing

    @Published private(set) var connection: VoiceUIState = .idle
    @Published private(set) var capture: CaptureStatus = .inactive
    @Published private(set) var history = AppHistory()
    @Published private(set) var pairing: PairingPresentation?
    @Published private(set) var message: String?
    @Published private(set) var pending = false
    @Published private(set) var stopping = false
    @Published private(set) var conversationActive = false
    @Published private(set) var wireReady = false
    @Published private(set) var isMuted = false
    @Published private var readiness: CaptureReady?
    @Published private var sourceCalls: [String: String] = [:]

    private let configuration: AppConfiguration?
    private let store: any CredentialStore
    private let factory: ConversationFactory
    private let pairingFactory: PairingFactory
    private let foreground: @MainActor () -> Bool
    private let now: () -> Date
    private var conversation: (any AppConversation)?
    private var observer: AppObserver?
    private var pairingAgent: DevicePairing?
    private var attemptedStart = false
    private var lifetime = UUID()
    private var action: Task<Void, Never>?
    private var cleanup: Task<Void, Never>?

    init(origin: String? = AppConfiguration.bundled(), store: any CredentialStore = KeychainCredentialStore(),
         foreground: @escaping @MainActor () -> Bool = { UIApplication.shared.applicationState == .active },
         now: @escaping () -> Date = Date.init,
         factory: @escaping ConversationFactory = { try NativeConversation(claim: $0, observer: $1) },
         pairingFactory: PairingFactory? = nil) {
        self.store = store; self.factory = factory; self.foreground = foreground; self.now = now
        self.pairingFactory = pairingFactory ?? { origin in
            DevicePairing(origin: origin, http: NativePairingHTTP(origin: origin), store: store, browser: SystemPairingBrowser())
        }
        do { configuration = try AppConfiguration(origin: origin) }
        catch { configuration = nil; message = AppCopy.error(AppIssue.configuration) }
        // No Keychain read, pairing, permission, connection or audio on init.
    }

    var configured: Bool { configuration != nil }
    var canStart: Bool { configured && !pending && !stopping && !conversationActive && pairing == nil }
    var canPair: Bool { configured && !pending && !stopping && !conversationActive }
    var canBegin: Bool {
        guard wireReady, !pending, !stopping, !isMuted else { return false }
        if case .inactive = capture { return true }
        return false // Responding alone never prohibits explicit barge-in.
    }
    var canEnd: Bool {
        guard !pending, !stopping, !isMuted else { return false }
        if case .armed = capture { return true }; return false
    }
    var canMute: Bool { wireReady && !pending && !stopping }
    var speakingPrompt: Bool {
        guard let readiness, !stopping, conversationActive else { return false }
        return conversation?.isCurrent(readiness) == true
    }
    var sourceStatus: String? {
        if sourceCalls.values.contains("read_calendar") { return "Проверяем календарь" }
        if sourceCalls.values.contains("read_assignments") { return "Проверяем задания" }
        return nil
    }

    private func launch(_ body: @escaping @MainActor (UUID) async throws -> Void) {
        guard action == nil, cleanup == nil else { return }
        let token = lifetime; pending = true; message = nil
        action = Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                // Stop can cancel this queued Task before it first runs. Fence
                // BEFORE body may read credentials/create audio/start pairing.
                try self.check(token)
                try await body(token)
            }
            catch {
                if self.lifetime == token, !Task.isCancelled {
                    self.requestStop(message: AppCopy.error(error))
                }
            }
            if self.lifetime == token { self.pending = false; self.action = nil }
        }
    }
    private func check(_ token: UUID) throws {
        try Task.checkCancellation()
        guard lifetime == token else { throw CancellationError() }
    }

    func pairDevice() {
        guard canPair, let configuration else { return }
        launch { [self] token in
            guard foreground() else { throw AppIssue.foreground }
            if let old = pairingAgent { await old.cancel(); try check(token) }
            let agent = pairingFactory(configuration.origin); pairingAgent = agent
            connection = .pairing
            let presentation = try await agent.begin(deviceName: "CampusPulse iPhone", now: now())
            try check(token); pairing = presentation
        }
    }
    func continuePairing() {
        guard !pending, !stopping, pairing != nil, let agent = pairingAgent else { return }
        launch { [self] token in
            guard foreground() else { throw AppIssue.foreground }
            try await agent.continueAfterBrowserApproval(now: now())
            try check(token)
            pairing = nil; pairingAgent = nil; connection = .idle
            message = "Устройство подключено. Нажмите «Начать разговор»."
            // Saving a credential never starts or resumes audio.
        }
    }
    func startConversation() {
        guard canStart, let configuration else { return }
        launch { [self] token in
            guard foreground() else { throw AppIssue.foreground }
            guard let credential = try store.load(for: configuration.origin) else { throw AppIssue.credentialMissing }
            guard credential.expiresAt > now() else { throw AppIssue.credentialExpired }
            let claim = VoiceClaim(origin: configuration.origin, credential: credential)
            // Even SAME claim reconnect joins audio/session before restart. A new
            // claim adopts a new actor only after the old audio.userStop joined.
            if let old = conversation { await old.stop(); try check(token) }
            if conversation?.claim != claim {
                observer?.deactivate()
                let observer = AppObserver()
                observer.onObservation = { [weak self] value, lifetime in self?.observe(value, token: lifetime) }
                observer.onTranscript = { [weak self] id, value, lifetime in
                    guard let self, self.lifetime == lifetime else { return }
                    self.history.replace(id: id, value: value)
                }
                conversation = try factory(claim, observer); self.observer = observer; attemptedStart = false
            }
            guard let conversation, let observer else { throw AppIssue.connection }
            history = AppHistory(); sourceCalls.removeAll(); wireReady = false; readiness = nil; isMuted = false
            observer.activate(lifetime: token)
            conversation.onCaptureStatus = { [weak self] value in
                guard let self, self.lifetime == token else { return }
                self.capture = value
                if case .armed = value {} else { self.readiness = nil }
            }
            conversation.onPause = { [weak self] _ in
                guard let self, self.lifetime == token else { return }
                // Separate OWNED cleanup task: this callback may run on W10's
                // pump/watcher. Never await userStop (and self-join) here.
                self.requestStop(message: "Аудио приостановлено. Проверьте наушники и начните разговор заново.")
            }
            conversationActive = true; connection = attemptedStart ? .reconnecting : .connecting
            let reconnect = attemptedStart; attemptedStart = true
            try await conversation.start(credential: credential, reconnect: reconnect)
            try check(token)
        }
    }
    func beginUtterance() {
        guard canBegin, let conversation else { return }
        launch { [self] token in
            let ready = try await conversation.begin()
            try check(token)
            guard conversation.isCurrent(ready) else { throw AudioFailure.retiredTicket }
            readiness = ready
        }
    }
    func endUtterance() {
        guard canEnd, let conversation else { return }
        readiness = nil
        launch { [self] token in
            try await conversation.end() // Tail/mailbox + real wire input.end.
            try check(token) // No second Begin until this action has returned.
        }
    }
    func toggleMute() {
        guard canMute, let conversation else { return }
        let desired = !isMuted; readiness = nil
        launch { [self] token in
            let ready = try await conversation.mute(desired)
            try check(token); isMuted = desired
            if let ready {
                guard conversation.isCurrent(ready) else { throw AudioFailure.retiredTicket }
                readiness = ready
            }
        }
    }

    // Always callable, including permission/connecting/arming/responding/error.
    // Fence immediately; cleanup owns and joins the previous control action.
    func requestStop(message finalMessage: String? = nil) {
        guard cleanup == nil else { return }
        lifetime = UUID(); let token = lifetime
        let oldAction = action, oldConversation = conversation, oldPairing = pairingAgent
        oldAction?.cancel(); observer?.deactivate()
        readiness = nil; wireReady = false; sourceCalls.removeAll()
        pairing = nil; pairingAgent = nil; stopping = true; pending = false; connection = .stopping
        cleanup = Task { @MainActor [weak self] in
            await oldConversation?.stop()
            await oldPairing?.cancel()
            await oldAction?.value
            guard let self, self.lifetime == token else { return }
            self.capture = .inactive; self.isMuted = false; self.conversationActive = false
            self.connection = finalMessage == nil ? .idle : .error
            self.message = finalMessage; self.stopping = false; self.action = nil; self.cleanup = nil
        }
    }
    private func observe(_ value: VoiceObservation, token: UUID) {
        guard token == lifetime, conversationActive, !stopping else { return }
        history.append(value); connection = value.state
        if value.type == "ready" { wireReady = true }
        if value.type == "tool_started", let id = value.callID, let tool = value.tool,
           ["read_calendar", "read_assignments"].contains(tool), sourceCalls.count < 4 { sourceCalls[id] = tool }
        if value.type == "tool_finished", let id = value.callID { sourceCalls.removeValue(forKey: id) }
        if value.type == "error" || value.type == "closed" {
            requestStop(message: value.type == "error" ? AppCopy.error(AppIssue.connection) : nil)
        }
    }
    // Deliberately no scene-phase observer/restoration/reconnect timer. Moving
    // an already authorized conversation to background alone does not stop it.
}

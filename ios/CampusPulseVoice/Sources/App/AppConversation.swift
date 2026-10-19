import Foundation
import UIKit

@MainActor protocol AppConversation: AnyObject {
    var claim: VoiceClaim { get }
    var onCaptureStatus: (@MainActor (CaptureStatus) -> Void)? { get set }
    var onPause: (@MainActor (AudioPauseReason) -> Void)? { get set }
    func start(credential: DeviceCredential, reconnect: Bool) async throws
    func stop() async
    func begin() async throws -> CaptureReady
    func end() async throws
    func mute(_ value: Bool) async throws -> CaptureReady?
    func isCurrent(_ ready: CaptureReady) -> Bool
}

@MainActor final class NativeConversation: AppConversation {
    let claim: VoiceClaim
    private let audio: AudioController
    private let session: VoiceSession
    init(claim: VoiceClaim, observer: AppObserver) throws {
        self.claim = claim
        let audio = AudioController.native()
        let session = VoiceSession(claim: claim, audio: audio, observer: observer)
        try audio.bind(session)
        self.audio = audio; self.session = session
    }
    var onCaptureStatus: (@MainActor (CaptureStatus) -> Void)? {
        get { audio.onCaptureStatus } set { audio.onCaptureStatus = newValue }
    }
    var onPause: (@MainActor (AudioPauseReason) -> Void)? {
        get { audio.onPause } set { audio.onPause = newValue }
    }
    func start(credential: DeviceCredential, reconnect: Bool) async throws {
        guard UIApplication.shared.applicationState == .active else { throw AppIssue.foreground }
        guard credential.expiresAt > Date(), VoiceClaim(origin: claim.origin, credential: credential) == claim else {
            throw AppIssue.credentialExpired
        }
        do {
            try await audio.userStart()
            try Task.checkCancellation()
            guard credential.expiresAt > Date() else { throw AppIssue.credentialExpired }
            try await session.startNative(credential: credential, reconnect: reconnect)
        } catch { await audio.userStop(); throw error }
    }
    func stop() async { await audio.userStop() }
    func begin() async throws -> CaptureReady { try await audio.userBeginUtterance() }
    func end() async throws { try await audio.userEndUtterance() }
    func mute(_ value: Bool) async throws -> CaptureReady? { try await audio.userMute(value) }
    func isCurrent(_ ready: CaptureReady) -> Bool { audio.isCurrent(ready) }
}

@MainActor struct SystemPairingBrowser: PairingBrowser {
    func openVerificationPage(_ url: URL) async throws {
        guard UIApplication.shared.applicationState == .active else { throw AppIssue.foreground }
        let opened = await withCheckedContinuation { continuation in
            UIApplication.shared.open(url, options: [:]) { continuation.resume(returning: $0) }
        }
        guard opened else { throw AppIssue.pairing }
    }
}

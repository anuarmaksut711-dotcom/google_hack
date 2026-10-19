
















import Foundation

struct ApplicationError: Error, Sendable, CustomNSError {
    let failure: AudioFailure
    init(_ failure: AudioFailure) { self.failure = failure }
    var errorUserInfo: [String: Any] {
        [NSLocalizedDescriptionKey: "Audio is paused. Use the conversation controls to restart."]
    }
}

import Foundation
import XCTest
@testable import CampusPulseVoice

@MainActor final class AppPresentationTests: XCTestCase {
    func testObserverFencesSuspendCloseAndOldLifetime() {
        let observer = AppObserver(); let old = UUID(), current = UUID(); var observations = 0; var transcripts = 0
        observer.onObservation = { _, _ in observations += 1 }
        observer.onTranscript = { _, _, _ in transcripts += 1 }
        let value = WireState.Transcript(speaker: "user", generation: 0, revision: 0, final: false, text: "hi")
        let event = VoiceObservation(state: .listening, type: "ready", runID: nil, sessionID: nil,
            callID: nil, tool: nil, status: nil, durationMS: nil, elapsedMS: nil, reason: nil)
        observer.activate(lifetime: UUID()); observer.begin(epoch: old)
        observer.replaceTranscript(id: "a", value: value, epoch: old); XCTAssertEqual(transcripts, 1)
        observer.suspendData(epoch: old); observer.replaceTranscript(id: "b", value: value, epoch: old)
        observer.observe(event, epoch: old); XCTAssertEqual(observations, 0)
        observer.deactivate(); observer.begin(epoch: old); observer.observe(event, epoch: old)
        observer.activate(lifetime: UUID()); observer.begin(epoch: current)
        observer.close(epoch: old); observer.observe(event, epoch: current)
        XCTAssertEqual(observations, 1); XCTAssertEqual(transcripts, 1)
        observer.close(epoch: current); observer.observe(event, epoch: current); XCTAssertEqual(observations, 1)
    }
    func testHistoryBoundsRevisionsPlainTextAndAllowedTools() {
        var history = AppHistory()
        for index in 0..<100 {
            history.replace(id: "t\(index)", value: WireState.Transcript(speaker: "assistant", generation: 1,
                revision: 0, final: false, text: String(repeating: "x", count: 5000)))
            history.append(VoiceObservation(state: .listening, type: "tool_finished", runID: String(repeating: "r", count: 100),
                sessionID: nil, callID: "call_\(index)", tool: "read_calendar", status: "success", durationMS: 12, elapsedMS: 20, reason: nil))
        }
        XCTAssertEqual(history.transcripts.count, 32); XCTAssertEqual(history.statuses.count, 32)
        XCTAssertEqual(history.transcripts.first?.text.count, 4096)
        history.replace(id: "t99", value: WireState.Transcript(speaker: "assistant", generation: 1, revision: 1,
            final: true, text: "<a href='https://invalid'>**данные**</a>\u{0000}"))
        XCTAssertEqual(history.transcripts.count, 32)
        XCTAssertEqual(history.transcripts.last?.text, "<a href='https://invalid'>**данные**</a>")
        let count = history.statuses.count
        history.append(VoiceObservation(state: .listening, type: "tool_finished", runID: nil, sessionID: nil,
            callID: nil, tool: "apply_plan", status: "success", durationMS: nil, elapsedMS: nil, reason: nil))
        XCTAssertEqual(history.statuses.count, count)
        XCTAssertTrue(history.statuses.last?.title.contains("вызов завершён") == true)
        XCTAssertFalse(history.statuses.last?.title.contains("актуален") == true)
    }
    func testSafeErrorMappingNeverRendersRawError() {
        struct UnsafeError: LocalizedError { var errorDescription: String? { "secret_and_private_event" } }
        XCTAssertFalse(AppCopy.error(UnsafeError()).contains("secret_and_private_event"))
        XCTAssertNotEqual(AppCopy.error(AppIssue.credentialExpired), AppCopy.error(AppIssue.credentialMissing))
        XCTAssertNotEqual(AppCopy.capture(.inactive), AppCopy.capture(.failed(.graph)))
        XCTAssertNotEqual(AppCopy.connection(.connecting), AppCopy.connection(.listening))
    }
}

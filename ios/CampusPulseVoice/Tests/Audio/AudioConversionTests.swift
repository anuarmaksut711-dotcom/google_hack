import AVFoundation
import XCTest
@testable import CampusPulseVoice

enum SyntheticAudio {
    static func buffer(rate: Double, channels: AVAudioChannelCount = 1, frames: Int,
                       capacity: Int? = nil) throws -> AVAudioPCMBuffer {
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: rate, channels: channels))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(capacity ?? frames)))
        buffer.frameLength = AVAudioFrameCount(frames)
        let planes = try XCTUnwrap(buffer.floatChannelData)
        for channel in 0..<Int(channels) {
            for frame in 0..<frames { planes[channel][frame] = Float(sin(Double(frame) * 0.01) * 0.25) }
        }
        return buffer
    }
}

final class AudioConversionTests: XCTestCase {
    func testCapacityUsesRealRateRatioAndRejectsUnboundedInputs() throws {
        XCTAssertEqual(try AudioBufferHelpers.capacity(frames: 960, inputRate: 48000, outputRate: 16000), 384)
        XCTAssertEqual(try AudioBufferHelpers.capacity(frames: 441, inputRate: 44100, outputRate: 16000), 224)
        XCTAssertThrowsError(try AudioBufferHelpers.capacity(frames: 9000, inputRate: 48000, outputRate: 16000))
        XCTAssertThrowsError(try AudioBufferHelpers.capacity(frames: 960, inputRate: .nan, outputRate: 16000))
    }
    func testProducedLengthUsesFrameLengthAndExplicitLittleEndian() throws {
        let buffer = try SyntheticAudio.buffer(rate: 16000, frames: 5, capacity: 1024)
        let samples = try XCTUnwrap(buffer.floatChannelData?[0])
        for (index, value) in [Float(-1), -0.5, 0, 0.5, 1].enumerated() { samples[index] = value }
        XCTAssertEqual(try AudioBufferHelpers.pcm16LE(buffer), Data([0, 128, 0, 192, 0, 0, 0, 64, 255, 127]))
    }
    func testStereo48kConversionAndPartialFinalChunkPreserveDuration() throws {
        let input = try SyntheticAudio.buffer(rate: 48000, channels: 2, frames: 961)
        let converter = try CaptureConverter(format: input.format)
        var total = Data()
        for _ in 0..<10 { total.append(try converter.convert(input)) }
        total.append(try converter.convert(nil, end: true))


        XCTAssertLessThanOrEqual(abs(total.count / 2 - 3203), 4)
        var chunker = PCMChunker(); let chunks = try chunker.append(total)
        XCTAssertTrue(chunks.allSatisfy { $0.count == 640 && $0.count <= 1280 })
        let tail = chunker.finish()
        XCTAssertEqual(chunks.reduce(0) { $0 + $1.count } + (tail?.count ?? 0), total.count)
    }
    func test44100NonIntegralResamplingAndHardwareFormatChange() throws {
        let input = try SyntheticAudio.buffer(rate: 44100, channels: 2, frames: 887)
        let converter = try CaptureConverter(format: input.format)
        var count = 0
        for _ in 0..<10 { count += try converter.convert(input).count }
        count += try converter.convert(nil, end: true).count
        XCTAssertLessThanOrEqual(abs(count / 2 - 3218), 4)
        let changed = try SyntheticAudio.buffer(rate: 48000, frames: 960)
        XCTAssertThrowsError(try converter.convert(changed))
        let rebuilt = try CaptureConverter(format: changed.format)
        XCTAssertFalse(try rebuilt.convert(changed).isEmpty)
    }
    func testPlaybackIs24kMonoWithExactSampleCountNotHardwareRelabeling() throws {
        let raw = Data([0, 128, 255, 255, 0, 0, 1, 0, 255, 127, 52, 18, 204, 237, 42, 0])
        let buffer = try AudioBufferHelpers.playbackBuffer(raw)
        XCTAssertEqual(buffer.format.sampleRate, 24000)
        XCTAssertEqual(buffer.format.channelCount, 1); XCTAssertEqual(buffer.frameLength, 8)
        XCTAssertEqual(try AudioBufferHelpers.pcm16LE(buffer), raw)
        XCTAssertThrowsError(try AudioBufferHelpers.playbackBuffer(Data([1])))
    }
    func testMailboxCountsInFlightAndFailsExplicitlyAtLimit() throws {
        let box = CaptureMailbox(), ticket = CaptureTicket(epoch: UUID(), connection: 1, utterance: UUID()), revision = UUID()
        let input = try SyntheticAudio.buffer(rate: 16000, frames: 320)
        try box.prepare(ticket: ticket, revision: revision, format: input.format)
        box.ingest(input, ticket: ticket, revision: revision)
        let delivery = try XCTUnwrap(box.take())
        for _ in 0..<9 { box.ingest(input, ticket: ticket, revision: revision) }
        XCTAssertEqual(box.bytesRetained, 6400)
        box.ingest(input, ticket: ticket, revision: revision)
        XCTAssertEqual(box.error(), .capacity)
        XCTAssertNil(box.take()); XCTAssertEqual(box.bytesRetained, delivery.pcm.count)
        box.acknowledge(delivery.id); XCTAssertTrue(box.drained)
    }
    func testDiscardRetainsInFlightChargeAndIgnoresRetiredTapRevision() throws {
        let box = CaptureMailbox(), ticket = CaptureTicket(epoch: UUID(), connection: 1, utterance: UUID()), old = UUID(), next = UUID()
        let input = try SyntheticAudio.buffer(rate: 16000, frames: 320)
        try box.prepare(ticket: ticket, revision: old, format: input.format)
        box.ingest(input, ticket: ticket, revision: old)
        let delivery = try XCTUnwrap(box.take())
        box.discard(); XCTAssertFalse(box.drained); XCTAssertEqual(box.bytesRetained, 640)
        try box.prepare(ticket: ticket, revision: next, format: input.format)
        box.ingest(input, ticket: ticket, revision: old)
        box.acknowledge(delivery.id); XCTAssertTrue(box.drained)
        box.ingest(input, ticket: ticket, revision: next)
        XCTAssertNotNil(box.take())
    }
    func testFinishEmitsActualShortPartialWithoutPadding() throws {
        let box = CaptureMailbox(), ticket = CaptureTicket(epoch: UUID(), connection: 1, utterance: UUID()), revision = UUID()
        let input = try SyntheticAudio.buffer(rate: 16000, frames: 137)
        try box.prepare(ticket: ticket, revision: revision, format: input.format)
        box.ingest(input, ticket: ticket, revision: revision)
        XCTAssertNil(box.take())
        try box.finish(ticket: ticket, revision: revision)
        let tail = try XCTUnwrap(box.take()); XCTAssertEqual(tail.pcm.count, 274)
        box.acknowledge(tail.id); XCTAssertTrue(box.drained)
    }
}

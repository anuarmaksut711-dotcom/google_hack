
















import AVFoundation
import Foundation

enum AudioBufferHelpers {
    static func validHardware(_ format: AVAudioFormat) -> Bool {
        [.pcmFormatFloat32, .pcmFormatFloat64, .pcmFormatInt16, .pcmFormatInt32].contains(format.commonFormat)
            && format.sampleRate.isFinite && (8000...192000).contains(format.sampleRate)
            && (1...8).contains(format.channelCount)
            && (1...64).contains(format.streamDescription.pointee.mBytesPerFrame)
    }
    static func capacity(frames: Int, inputRate: Double, outputRate: Double) throws -> Int {
        guard (1...8192).contains(frames), inputRate.isFinite, outputRate.isFinite,
              inputRate >= 8000, outputRate > 0 else { throw AudioFailure.conversion }
        let result = ceil(Double(frames) * outputRate / inputRate) + 64
        guard result <= 4096 else { throw AudioFailure.capacity }
        return Int(result)
    }
    static func slice(_ input: AVAudioPCMBuffer, offset: Int, frames: Int) -> AVAudioPCMBuffer? {
        guard frames > 0, offset >= 0, offset + frames <= Int(input.frameLength),
              let copy = AVAudioPCMBuffer(pcmFormat: input.format, frameCapacity: AVAudioFrameCount(frames)) else { return nil }
        copy.frameLength = AVAudioFrameCount(frames)
        let source = UnsafeMutableAudioBufferListPointer(input.mutableAudioBufferList)
        let target = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
        let stride = Int(input.format.streamDescription.pointee.mBytesPerFrame)
        guard stride > 0, source.count == target.count else { return nil }
        for index in 0..<source.count {
            let bytes = frames * stride
            guard let src = source[index].mData, let dst = target[index].mData,
                  Int(source[index].mDataByteSize) >= (offset + frames) * stride,
                  Int(target[index].mDataByteSize) >= bytes else { return nil }
            dst.copyMemory(from: src.advanced(by: offset * stride), byteCount: bytes)
        }
        return copy
    }
    static func pcm16LE(_ buffer: AVAudioPCMBuffer) throws -> Data {
        guard buffer.format.commonFormat == .pcmFormatFloat32, buffer.format.channelCount == 1,
              let channel = buffer.floatChannelData?[0] else { throw AudioFailure.invalidPCM }
        var bytes = Data(capacity: Int(buffer.frameLength) * 2)
        for index in 0..<Int(buffer.frameLength) {
            let value = channel[index]
            guard value.isFinite else { throw AudioFailure.invalidPCM }
            let clipped = max(-1, min(1, value))
            let signed = Int16(max(-32768, min(32767, Int((clipped * 32768).rounded()))))
            let sample = UInt16(bitPattern: signed)
            bytes.append(UInt8(truncatingIfNeeded: sample))
            bytes.append(UInt8(truncatingIfNeeded: sample >> 8))
        }
        return bytes
    }
    static func playbackBuffer(_ pcm: Data) throws -> AVAudioPCMBuffer {
        guard pcm.count >= 2, pcm.count <= 1920, pcm.count % 2 == 0,
              let format = AVAudioFormat(standardFormatWithSampleRate: 24000, channels: 1),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(pcm.count / 2)),
              let channel = buffer.floatChannelData?[0] else { throw AudioFailure.invalidPCM }
        buffer.frameLength = AVAudioFrameCount(pcm.count / 2)
        let bytes = Array(pcm)
        for index in 0..<Int(buffer.frameLength) {
            let sample = Int16(bitPattern: UInt16(bytes[index * 2]) | UInt16(bytes[index * 2 + 1]) << 8)
            channel[index] = Float(sample) / 32768
        }
        return buffer
    }
}



final class CaptureConverter {
    let inputFormat: AVAudioFormat
    private let converter: AVAudioConverter
    init(format: AVAudioFormat) throws {
        guard AudioBufferHelpers.validHardware(format),
              let output = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1),
              let converter = AVAudioConverter(from: format, to: output) else { throw AudioFailure.conversion }
        inputFormat = format; self.converter = converter
        converter.downmix = true
        converter.primeMethod = .none
    }
    func convert(_ input: AVAudioPCMBuffer?, end: Bool = false) throws -> Data {
        if let input {
            guard input.format == inputFormat else { throw AudioFailure.formatChanged }
            guard input.frameLength > 0 else { return Data() }
        }
        let capacity = try input.map {
            try AudioBufferHelpers.capacity(frames: Int($0.frameLength), inputRate: inputFormat.sampleRate, outputRate: 16000)
        } ?? 1024
        var offset = 0, failedInput = false
        var result = Data()
        for _ in 0..<16 {
            guard let output = AVAudioPCMBuffer(pcmFormat: converter.outputFormat, frameCapacity: AVAudioFrameCount(capacity)) else {
                throw AudioFailure.conversion
            }
            var error: NSError?
            let status = converter.convert(to: output, error: &error) { requested, state in
                if let input, offset < Int(input.frameLength) {
                    let count = min(Int(requested), Int(input.frameLength) - offset)
                    guard let slice = AudioBufferHelpers.slice(input, offset: offset, frames: count) else {
                        failedInput = true; state.pointee = .noDataNow; return nil
                    }
                    offset += count; state.pointee = .haveData; return slice
                }
                state.pointee = end ? .endOfStream : .noDataNow
                return nil
            }
            guard error == nil, !failedInput, status != .error else { throw AudioFailure.conversion }


            let bytes = try AudioBufferHelpers.pcm16LE(output)
            guard result.count + bytes.count <= 6400 else { throw AudioFailure.capacity }
            result.append(bytes)
            if status == .endOfStream || (status == .inputRanDry && !end) { return result }
            if status == .haveData || (end && status == .inputRanDry) { continue }
            throw AudioFailure.conversion
        }
        throw AudioFailure.conversion
    }
}

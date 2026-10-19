import Foundation

struct AudioEnvelope: Equatable, Sendable {
    enum Kind: UInt8, Sendable { case input = 1, output = 2 }
    let kind: Kind
    let connection: UInt32
    let generation: UInt32
    let sequence: UInt32
    let pcm: Data
    func validate() throws {
        guard connection > 0, (kind == .input ? generation == 0 : generation > 0) else { throw VoiceFailure.invalidFrame }
        guard pcm.count >= 2, pcm.count <= (kind == .input ? 1280 : 1920), pcm.count % 2 == 0 else { throw VoiceFailure.invalidLength }
    }
    func encode() throws -> Data {
        try validate()
        var bytes: [UInt8] = [0x43, 0x50, 0x56, 0x41, 1, kind.rawValue, 0, 24]
        for value in [connection, generation, sequence, UInt32(pcm.count)] {
            bytes += [UInt8(truncatingIfNeeded: value >> 24), UInt8(truncatingIfNeeded: value >> 16),
                      UInt8(truncatingIfNeeded: value >> 8), UInt8(truncatingIfNeeded: value)]
        }
        return Data(bytes) + pcm
    }
    static func decode(_ data: Data, expected: Kind) throws -> AudioEnvelope {
        guard (24...1944).contains(data.count) else { throw VoiceFailure.invalidLength }
        let b = Array(data)
        guard Array(b[0..<4]) == [0x43, 0x50, 0x56, 0x41], b[6] == 0, b[7] == 24,
              let kind = Kind(rawValue: b[5]), kind == expected else { throw VoiceFailure.invalidFrame }
        guard b[4] == 1 else { throw VoiceFailure.unsupportedVersion }
        func u32(_ i: Int) -> UInt32 {
            (UInt32(b[i]) << 24) | (UInt32(b[i+1]) << 16) | (UInt32(b[i+2]) << 8) | UInt32(b[i+3])
        }
        guard u32(20) == UInt32(data.count - 24) else { throw VoiceFailure.invalidLength }
        let frame = AudioEnvelope(kind: kind, connection: u32(8), generation: u32(12), sequence: u32(16), pcm: Data(b[24...]))
        try frame.validate(); return frame
    }
    var samples: [Int16] {
        let b = Array(pcm)
        return stride(from: 0, to: b.count, by: 2).map { Int16(bitPattern: UInt16(b[$0]) | UInt16(b[$0+1]) << 8) }
    }
}

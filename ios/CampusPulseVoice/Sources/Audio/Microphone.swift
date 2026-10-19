// Copyright 2025 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// Modified for CampusPulse, 2026-10-19: 20-ms requested hardware tap; bounded
// owned conversion in callback, no escaping tap buffer/unbounded stream/task;
// revision fences and idempotent removal. See UPSTREAM.json.

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
        // Requested size is a hint. Converter/chunker handle actual frameLength,
        // hardware rate, channels and partial output; W02 frames remain <=40ms.
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

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
// Modified for CampusPulse, 2026-10-19: bounded scheduled/in-flight accounting,
// played-back completion, cancellation/flush fences, mixer conversion; UPSTREAM.json.

import AVFoundation
import Foundation

@MainActor protocol PlaybackScheduling: AnyObject {
    // Callback reports physical native completion, never enqueue or waiter cancel.
    func schedule(_ pcm: Data, completion: @escaping @Sendable (PlaybackOutcome) -> Void) throws
    func clear()
    func shutdown()
}

// All native bookkeeping, including retiring buffers, is lock-protected. The
// AV callback retires its exact entry BEFORE publishing actual consumption.
// There is no actor-hop Task per completion and no credit for waiter cancellation.
final class NativePlaybackBook: @unchecked Sendable {
    private struct Pending {
        let receipt: PlaybackReceipt
        var retiring = false
    }
    private let lock = NSLock()
    private var pending: [UUID: Pending] = [:]
    private var seenIDs: Set<UUID> = []
    private var epoch: UUID?
    private var connection: UInt32?
    private var minimumSerial: UInt64 = 0
    private var generation: UInt32 = 0
    private var nextSequence: UInt64 = 0
    private var generationMustAdvance = false
    private var closed = false
    private var poisoned = false
    private let onFailure: @Sendable () -> Void
    init(onFailure: @escaping @Sendable () -> Void) { self.onFailure = onFailure }
    var usage: (bytes: Int, packets: Int) {
        lock.lock(); defer { lock.unlock() }
        return (pending.values.reduce(0) { $0 + $1.receipt.admission.pcmBytes }, pending.count)
    }
    func reserve(_ frame: AudioEnvelope, admission: PlaybackAdmission) throws -> PlaybackReceipt {
        try frame.validate()
        lock.lock(); defer { lock.unlock() }
        let ticket = admission.ticket
        guard !closed, !poisoned, frame.kind == .output,
              admission.connection == frame.connection, admission.sequence == frame.sequence,
              admission.pcmBytes == frame.pcm.count, ticket.generation == frame.generation,
              epoch == nil || epoch == ticket.epoch, connection == nil || connection == frame.connection,
              ticket.serial == minimumSerial, !seenIDs.contains(admission.id) else { throw AudioFailure.retiredTicket }
        guard seenIDs.count < 65536, pending.count < 32,
              pending.values.reduce(0, { $0 + $1.receipt.admission.pcmBytes }) + frame.pcm.count <= 9600 else {
            throw AudioFailure.capacity
        }
        if frame.generation == generation {
            guard !generationMustAdvance, UInt64(frame.sequence) == nextSequence else { throw AudioFailure.retiredTicket }
        } else {
            // W09 validates every generation event. This port observes PCM
            // only: valid intervening generations can contain no audio at all.
            guard frame.generation > generation, frame.sequence == 0 else { throw AudioFailure.retiredTicket }
        }
        epoch = ticket.epoch; connection = frame.connection; generation = frame.generation
        generationMustAdvance = false
        nextSequence = UInt64(frame.sequence) + 1; seenIDs.insert(admission.id)
        let receipt = PlaybackReceipt(admission: admission, consumption: PlaybackConsumption())
        pending[admission.id] = Pending(receipt: receipt)
        return receipt
    }
    func complete(_ id: UUID, outcome: PlaybackOutcome) {
        lock.lock()
        guard var entry = pending[id], !entry.retiring else { lock.unlock(); return }
        let receipt = entry.receipt
        if outcome == .failed {
            // Failure may leave unknown native ownership. Keep its charge until
            // the synchronous clear/flush confirms unscheduling.
            entry.retiring = true; pending[id] = entry; poisoned = true
        } else {
            pending.removeValue(forKey: id)
            if outcome == .cancelled { poisoned = true }
        }
        lock.unlock()
        receipt.consumption.resolve(outcome)
        if outcome != .consumed { onFailure() }
    }
    func beginFlush(epoch value: UUID, nextSerial: UInt64) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard !closed, epoch == nil || epoch == value, nextSerial > minimumSerial else { return false }
        epoch = value; minimumSerial = nextSerial
        generationMustAdvance = true
        for id in Array(pending.keys) { pending[id]?.retiring = true }
        return true
    }
    func beginAbort() {
        lock.lock(); poisoned = true
        for id in Array(pending.keys) { pending[id]?.retiring = true }
        lock.unlock()
    }
    func beginClose() -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard !closed else { return false }; closed = true
        for id in Array(pending.keys) { pending[id]?.retiring = true }
        return true
    }
    // Call ONLY after native clear/shutdown returns. Callbacks during clear see
    // retiring=true and cannot publish a fictitious played-back result.
    func confirmClear(failedID: UUID? = nil) {
        lock.lock()
        let receipts = pending.values.map { $0.receipt }
        pending.removeAll(); lock.unlock()
        for receipt in receipts {
            receipt.consumption.resolve(receipt.admission.id == failedID ? .failed : .cancelled)
        }
    }
}

@MainActor final class AudioPlayer {
    private let scheduler: any PlaybackScheduling
    private let book: NativePlaybackBook
    init(scheduler: any PlaybackScheduling, onFailure: @escaping @Sendable () -> Void = {}) {
        self.scheduler = scheduler; book = NativePlaybackBook(onFailure: onFailure)
    }
    var retainedBytes: Int { book.usage.bytes }
    var retainedPackets: Int { book.usage.packets }
    func scheduleOutput(_ frame: AudioEnvelope, admission: PlaybackAdmission) throws -> PlaybackReceipt {
        let receipt = try book.reserve(frame, admission: admission)
        let bookkeeping = book, id = admission.id
        do {
            // SYNCHRONOUS native enqueue in MainActor caller order. No Task,
            // waiter, dataConsumed/dataRendered or early success here.
            try scheduler.schedule(frame.pcm) { bookkeeping.complete(id, outcome: $0) }
            return receipt
        } catch {
            // A scheduler can throw after an uncertain enqueue. Charge remains
            // through physical clear, then fail; never return a success receipt.
            book.beginAbort(); scheduler.clear(); book.confirmClear(failedID: id)
            throw error
        }
    }
    func flush(epoch: UUID, nextSerial: UInt64) {
        guard book.beginFlush(epoch: epoch, nextSerial: nextSerial) else { return }
        scheduler.clear() // Synchronous stop unschedules even currently rendering PCM.
        book.confirmClear()
    }
    func shutdown(beforeNativeClear: @MainActor () -> Void = {}) {
        guard book.beginClose() else { return }
        beforeNativeClear()
        scheduler.shutdown()
        book.confirmClear()
    }
}

@MainActor final class NativePlaybackScheduler: PlaybackScheduling {
    private let engine: AVAudioEngine
    private let node = AVAudioPlayerNode()
    private let routeMatches: @MainActor () -> Bool
    private var closed = false
    init(engine: AVAudioEngine, routeMatches: @escaping @MainActor () -> Bool) throws {
        self.engine = engine; self.routeMatches = routeMatches
        let hardware = engine.outputNode.outputFormat(forBus: 0)
        guard AudioBufferHelpers.validHardware(hardware),
              let format = AVAudioFormat(standardFormatWithSampleRate: 24000, channels: 1) else { throw AudioFailure.graph }
        engine.attach(node)
        // Continuous mono24k -> actual route sample-rate/channel conversion.
        // Leave mixer->output automatic; never relabel hardware as 24k.
        engine.connect(node, to: engine.mainMixerNode, format: format)
    }
    func schedule(_ pcm: Data, completion: @escaping @Sendable (PlaybackOutcome) -> Void) throws {
        guard !closed, routeMatches() else { throw AudioFailure.formatChanged }
        let buffer = try AudioBufferHelpers.playbackBuffer(pcm)
        if !engine.isRunning { try engine.start() }
        node.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { _ in completion(.consumed) }
        node.play()
    }
    func clear() { node.stop() }
    func shutdown() {
        guard !closed else { return }; closed = true; node.stop()
        engine.disconnectNodeOutput(node); engine.detach(node)
    }
}

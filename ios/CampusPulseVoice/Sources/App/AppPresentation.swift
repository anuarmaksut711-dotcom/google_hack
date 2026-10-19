import Foundation

struct TranscriptRow: Identifiable {
    let id: String
    let speaker: String
    let text: String
    let final: Bool
}

struct StatusRow: Identifiable {
    let id = UUID()
    let title: String
    let detail: String
}


struct AppHistory {
    private(set) var transcripts: [TranscriptRow] = []
    private(set) var statuses: [StatusRow] = []
    static func text(_ value: String, limit: Int) -> String {
        String(String.UnicodeScalarView(value.unicodeScalars.filter {
            !CharacterSet.controlCharacters.contains($0) || $0 == "\n"
        }.prefix(limit)))
    }
    mutating func replace(id: String, value: WireState.Transcript) {
        let row = TranscriptRow(id: Self.text(id, limit: 64), speaker: value.speaker == "user" ? "Вы" : "Ассистент",
                                text: Self.text(value.text, limit: 4096), final: value.final)
        if let index = transcripts.firstIndex(where: { $0.id == row.id }) { transcripts[index] = row }
        else { transcripts.append(row) }
        if transcripts.count > 32 { transcripts.removeFirst(transcripts.count - 32) }
    }
    mutating func append(_ value: VoiceObservation) {
        let title: String
        switch value.type {
        case "tool_started", "tool_finished":
            let tool: String
            switch value.tool {
            case "read_assignments": tool = "Чтение заданий"
            case "read_calendar": tool = "Чтение календаря"
            case "solve_repair": tool = "Расчёт расписания"
            default: return
            }
            let status: String
            if value.type == "tool_started" { status = "выполняется" }
            else {
                switch value.status {
                case "success": status = "вызов завершён"
                case "failed": status = "ошибка"
                case "unavailable": status = "недоступно"
                case "cancelled": status = "отменено"
                default: return
                }
            }
            title = tool + ": " + status
        case "ready": title = "Соединение готово"
        case "closed": title = "Соединение завершено"
        case "error": title = "Ошибка соединения"
        case "generation": title = AppCopy.connection(value.state)
        case "interrupted": title = "Ответ прерван"
        default: return
        }
        var parts: [String] = []
        if let id = value.runID { parts.append("Запуск: " + Self.text(id, limit: 64)) }
        if let id = value.callID { parts.append("Вызов: " + Self.text(id, limit: 64)) }
        if let time = value.elapsedMS { parts.append("От начала: \(time) мс") }
        if let time = value.durationMS { parts.append("Длительность: \(time) мс") }
        statuses.append(StatusRow(title: title, detail: parts.joined(separator: " · ")))
        if statuses.count > 32 { statuses.removeFirst(statuses.count - 32) }
    }
}



@MainActor final class AppObserver: VoiceObserver {
    private var lifetime: UUID?
    private var epoch: UUID?
    private var suspended = true
    var onObservation: ((VoiceObservation, UUID) -> Void)?
    var onTranscript: ((String, WireState.Transcript, UUID) -> Void)?
    func activate(lifetime: UUID) { self.lifetime = lifetime; epoch = nil; suspended = false }
    func deactivate() { lifetime = nil; epoch = nil; suspended = true }
    func begin(epoch: UUID) { guard lifetime != nil, self.epoch == nil else { return }; self.epoch = epoch }
    func observe(_ observation: VoiceObservation, epoch: UUID) {
        guard self.epoch == epoch, let lifetime,
              !suspended || observation.type == "closed" || observation.type == "error" else { return }
        onObservation?(observation, lifetime)
    }
    func replaceTranscript(id: String, value: WireState.Transcript, epoch: UUID) {
        guard self.epoch == epoch, !suspended, let lifetime else { return }
        onTranscript?(id, value, lifetime)
    }
    func suspendData(epoch: UUID) { if self.epoch == epoch { suspended = true } }
    func close(epoch: UUID) { if self.epoch == epoch { deactivate() } }
}

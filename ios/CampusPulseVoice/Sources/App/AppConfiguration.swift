import Foundation

enum AppIssue: Error {
    case configuration, credentialMissing, credentialExpired, pairing, foreground, connection
}

struct AppConfiguration {
    let origin: BackendOrigin
    init(origin value: String?) throws {
        guard let value, !value.isEmpty, !value.contains("$(") else { throw AppIssue.configuration }
        let origin = try BackendOrigin(value)
        let labels = origin.host.split(separator: ".")
        guard !labels.contains("example"), !labels.contains("invalid"),
              !labels.contains("localhost"), !origin.host.hasSuffix(".test") else { throw AppIssue.configuration }
        self.origin = origin
    }
    static func bundled() -> String? { Bundle.main.object(forInfoDictionaryKey: "CampusPulseBackendOrigin") as? String }
}

enum AppCopy {
    static func error(_ error: Error) -> String {
        if let issue = error as? AppIssue {
            switch issue {
            case .configuration: return "Укажите рабочий HTTPS адрес сервера в конфигурации приложения."
            case .credentialMissing: return "Сначала подключите устройство через браузер."
            case .credentialExpired: return "Срок доступа истёк. Подключите устройство заново."
            case .pairing: return "Подключение не подтверждено или срок кода истёк. Получите новый код."
            case .foreground: return "Откройте приложение и нажмите «Начать разговор»."
            case .connection: return "Соединение завершено. Нажмите «Начать разговор» для новой попытки."
            }
        }
        let failure = (error as? AudioFailure) ?? (error as? ApplicationError)?.failure
        if failure == .permissionDenied { return "Разрешите доступ к микрофону в настройках iPhone, затем начните разговор. Перейти в настройки можно вручную." }
        if failure != nil { return "Аудио остановлено. Проверьте наушники и начните разговор заново." }
        if let failure = error as? VoiceFailure {
            switch failure {
            case .configuration: return self.error(AppIssue.configuration)
            case .credentialUnavailable: return self.error(AppIssue.credentialMissing)
            case .pairingDenied: return self.error(AppIssue.pairing)
            default: break
            }
        }
        return self.error(AppIssue.connection)
    }
    static func connection(_ state: VoiceUIState) -> String {
        switch state {
        case .idle: return "Не подключено"
        case .pairing: return "Подтвердите устройство в браузере"
        case .connecting: return "Подключаемся"
        case .reconnecting: return "Подключаемся заново"
        case .listening: return "Соединение готово"
        case .responding: return "Ассистент отвечает"
        case .interrupted: return "Ответ прерван"
        case .stopping: return "Останавливаем разговор"
        case .error: return "Соединение недоступно"
        }
    }
    static func capture(_ status: CaptureStatus) -> String {
        switch status {
        case .inactive: return "Микрофон не записывает"
        case .arming: return "Подготавливаем микрофон"
        case .armed: return "Микрофон подготовлен"
        case .ending: return "Отправляем конец реплики"
        case .muted: return "Микрофон выключен"
        case .failed: return "Не удалось подготовить микрофон"
        }
    }
}

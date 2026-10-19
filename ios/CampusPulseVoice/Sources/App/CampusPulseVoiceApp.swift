import SwiftUI

@main @MainActor struct CampusPulseVoiceApp: App {
    @StateObject private var model = VoiceScreenModel()
    var body: some Scene { WindowGroup { VoiceScreen(model: model) } }
}

@MainActor struct VoiceScreen: View {
    @ObservedObject var model: VoiceScreenModel
    var body: some View {
        NavigationView {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("Голосовой помощник").font(.title2).bold()
                    Text("Читает задания и календарь, предлагает расписание. Изменения календаря и подтверждение плана здесь недоступны.")
                    Text(verbatim: AppCopy.connection(model.connection)).font(.headline)
                        .accessibilityLabel("Соединение. " + AppCopy.connection(model.connection))
                    Text(verbatim: AppCopy.capture(model.capture))
                    if let source = model.sourceStatus { Text(verbatim: source) }
                    if let message = model.message { Text(verbatim: message).foregroundColor(.red) }
                    GroupBox("Подключение устройства") {
                        VStack(alignment: .leading, spacing: 10) {
                            Button("Получить код и открыть браузер") { model.pairDevice() }.disabled(!model.canPair)
                            if let pairing = model.pairing {
                                Text("Введите код на открытой странице, проверьте устройство и подтвердите подключение. Затем вернитесь сюда.")
                                Text(verbatim: pairing.code).font(.title.monospaced()).textSelection(.enabled)
                                    .accessibilityLabel("Код подключения: " + pairing.code)
                                Text("Код действует до \(pairing.expiresAt.formatted(date: .omitted, time: .shortened))")
                                Link("Открыть страницу подтверждения", destination: pairing.verificationPage)
                                Button("Продолжить после подтверждения") { model.continuePairing() }
                                    .disabled(model.pending || model.stopping)
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                    VStack(alignment: .leading, spacing: 12) {
                        Button("Начать разговор") { model.startConversation() }.disabled(!model.canStart)
                        Button("Остановить разговор", role: .destructive) { model.requestStop() }

                        Divider()
                        Text("Для каждой реплики нажмите «Начать реплику», дождитесь «Говорите», затем нажмите «Завершить реплику». Можно начать реплику во время ответа ассистента.")
                        Button("Начать реплику") { model.beginUtterance() }.disabled(!model.canBegin)
                        if model.speakingPrompt { Text("Говорите. После фразы завершите реплику.").bold() }
                        Button("Завершить реплику") { model.endUtterance() }.disabled(!model.canEnd)
                        Button(model.isMuted ? "Включить микрофон" : "Выключить микрофон") { model.toggleMute() }
                            .disabled(!model.canMute)
                        if model.isMuted { Text("Чтобы завершить открытую реплику, включите микрофон. Остановить разговор можно сразу.") }
                        Text("Фразу «закончить разговор» также нужно отправить кнопкой «Завершить реплику». Кнопка «Остановить разговор» работает отдельно.").font(.footnote)
                    }
                    GroupBox("Последние реплики") {
                        VStack(alignment: .leading, spacing: 10) {
                            if model.history.transcripts.isEmpty { Text("Реплик пока нет.") }
                            ForEach(model.history.transcripts) { row in
                                VStack(alignment: .leading) {
                                    Text(verbatim: row.speaker + (row.final ? "" : " · распознаётся")).font(.caption)
                                    Text(verbatim: row.text).textSelection(.enabled)
                                }
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                    DisclosureGroup("Вызовы и время выполнения") {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Завершение вызова само по себе не подтверждает актуальность календаря или применение плана.").font(.footnote)
                            if model.history.statuses.isEmpty { Text("Вызовов пока нет.") }
                            ForEach(model.history.statuses) { row in
                                Text(verbatim: row.title)
                                if !row.detail.isEmpty { Text(verbatim: row.detail).font(.caption).textSelection(.enabled) }
                            }
                        }
                    }
                }.padding()
            }.navigationTitle("CampusPulse").navigationBarTitleDisplayMode(.inline)
        }.navigationViewStyle(.stack)
    }
}

"""Read-only chat progress, rendered from observed server events."""

SCRIPT = r"""
(() => {
  const tools = {
    read_assignments: ['Задания Moodle', 'Получаю задания Moodle'],
    read_schedule: ['Расписание пар', 'Проверяю расписание пар']
  };
  const seconds = value => new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 1})
    .format(value / 1000) + ' с';
  function observations(turn) {
    const source = turn.status === 'completed' && Array.isArray(turn.tool_trace) && turn.tool_trace.length ? turn.tool_trace :
      Array.isArray(turn.agent_progress?.tool_calls) ? turn.agent_progress.tool_calls :
      Array.isArray(turn.tool_trace) ? turn.tool_trace : [];
    return source.filter(call => Object.hasOwn(tools, call.tool)).slice(0, 12);
  }
  function result(call, working) {
    if (call.status === 'unknown') return 'Результат не получен';
    if (call.status === 'running') return working ? 'Выполняется' : 'Результат не получен';
    if (call.status === 'not_connected') return call.tool === 'read_schedule' ?
      'Расписание пар не заполнено' : 'Moodle не подключён';
    if (call.status === 'unavailable') return 'Данные сейчас недоступны';
    if (call.status !== 'completed') return 'Не удалось получить данные';
    const count = Number.isInteger(call.result_count) ? call.result_count : null;
    if (count === null) return 'Данные получены';
    return (call.tool === 'read_assignments' ? 'Получено записей: ' : 'Получено занятий: ') + count;
  }
  function phaseText(turn, calls) {
    if (turn.status === 'failed') return 'Не удалось подготовить ответ';
    if (turn.status === 'unknown') return 'Ответ не получен. Обновите состояние, чтобы проверить результат.';
    if (turn.status === 'completed') return 'Ответ подготовлен';
    const phase = turn.agent_progress?.phase;
    if (phase === 'composing') return 'Готовлю ответ по результатам проверки';
    return {queued: 'Запрос передан агенту', thinking: 'Агент выбирает нужные инструменты',
      tools: 'Агент получает данные из источников', failed: 'Не удалось продолжить работу агента'}[phase] ||
      'Ожидаю ответ агента';
  }
  function create(turn) {
    const node = document.createElement('section'); node.className = 'agent-work';
    node.setAttribute('aria-label', 'Действия агента');
    const details = document.createElement('details'); details.open = true;
    const summary = document.createElement('summary'), title = document.createElement('span');
    title.textContent = 'Действия агента';
    const badge = document.createElement('span'); badge.className = 'agent-work-badge'; badge.textContent = 'MCP';
    summary.append(title, badge);
    const phase = document.createElement('p'); phase.className = 'agent-work-phase';
    phase.setAttribute('role', 'status'); phase.setAttribute('aria-live', 'polite'); phase.setAttribute('aria-atomic', 'true');
    const list = document.createElement('ol'); list.className = 'agent-work-calls';
    details.append(summary, phase, list); node.append(details); update(node, turn); return node;
  }
  function update(node, turn) {
    const calls = observations(turn), working = turn.status === 'working';
    node.hidden = !working && calls.length === 0;
    const fingerprint = JSON.stringify([turn.status, turn.agent_progress?.phase, calls]);
    if (node.dataset.snapshot === fingerprint) return;
    node.dataset.snapshot = fingerprint;
    node.querySelector('.agent-work-badge').hidden = calls.length === 0;
    const phase = node.querySelector('.agent-work-phase'), text = phaseText(turn, calls);
    if (phase.textContent !== text) phase.textContent = text;
    node.dataset.working = String(working);
    const list = node.querySelector('.agent-work-calls'); list.replaceChildren();
    for (const call of calls) {
      const row = document.createElement('li'), icon = document.createElement('span'), body = document.createElement('div');
      const running = working && call.status === 'running', completed = call.status === 'completed';
      row.dataset.state = running ? 'running' : completed ? 'completed' : 'warning';
      icon.className = 'agent-work-icon'; icon.setAttribute('aria-hidden', 'true');
      icon.textContent = running ? '↻' : completed ? '✓' : '!';
      const name = document.createElement('strong'); name.textContent = tools[call.tool][running ? 1 : 0];
      const outcome = document.createElement('p'); outcome.className = 'agent-work-result';
      outcome.textContent = result(call, working);
      const meta = document.createElement('div'); meta.className = 'agent-work-meta';
      const method = document.createElement('code'); method.textContent = call.tool; meta.append(method);
      if (!running && Number.isInteger(call.duration_ms) && call.duration_ms >= 0) {
        const duration = document.createElement('span'); duration.textContent = seconds(call.duration_ms); meta.append(duration);
      }
      body.append(name, outcome, meta); row.append(icon, body); list.append(row);
    }
  }
  class Poller {
    constructor({read, valid, update, failed, interval = 1000}) {
      Object.assign(this, {read, valid, update, failed, interval}); this.revision = 0;
    }
    start(requestKey) {
      this.stop(); const revision = this.revision, deadline = Date.now() + 165000;
      const tick = async () => {
        if (revision !== this.revision || !this.valid()) return;
        let finished = false;
        try {
          const chat = await this.read();
          if (revision !== this.revision || !this.valid()) return;
          const turn = chat.turns?.find(item => item.request_key === requestKey);
          if (turn) { this.update(chat, turn); finished = turn.status !== 'working'; }
        } catch (error) {
          if (revision !== this.revision || !this.valid()) return;
          this.failed(error); finished = error.status === 403;
        }
        if (!finished && revision === this.revision && this.valid() && Date.now() < deadline)
          this.timer = setTimeout(tick, this.interval);
      };
      this.timer = setTimeout(tick, this.interval);
    }
    stop() { ++this.revision; clearTimeout(this.timer); }
  }
  window.CampusPulseStudyTrace = {create, update, Poller};
})();
"""

STYLES = r"""
.chat-message .agent-work { margin: 0 0 20px; max-width: 620px; }
.chat-message .agent-work details { margin: 0; padding: 0 16px; border: 1px solid var(--line); border-radius: 12px; background: var(--surface); color: var(--ink); }
.agent-work summary { min-height: 44px; padding: 12px 0; cursor: pointer; font-size: 13px; font-weight: 600; }
.agent-work summary > span:first-child { margin-left: 3px; }
.agent-work-badge { margin-left: 10px; padding: 2px 6px; border: 1px solid var(--line); border-radius: 5px; color: var(--muted); font-size: 10px; font-weight: 500; letter-spacing: .04em; }
.chat-message .agent-work .agent-work-phase { margin: 0 0 12px; color: var(--muted); font-size: 12px; line-height: 1.6; }
.agent-work-calls { list-style: none; margin: 0; padding: 0; }
.agent-work-calls > li { display: flex; align-items: flex-start; gap: 10px; padding: 12px 0; border-top: 1px solid var(--line); }
.agent-work-calls > li > div { flex: 1; min-width: 0; }
.agent-work-calls strong { font-size: 13px; font-weight: 550; line-height: 1.6; }
.agent-work-icon { display: grid; place-items: center; width: 22px; height: 22px; flex-shrink: 0; border-radius: 50%; background: var(--subtle); font-size: 13px; color: var(--muted); }
.agent-work-calls [data-state=completed] .agent-work-icon { color: var(--theme-success); background: var(--theme-success-surface); }
.agent-work-calls [data-state=warning] .agent-work-icon { color: var(--theme-warning); background: var(--theme-warning-surface); }
.agent-work-calls [data-state=running] .agent-work-icon { color: var(--accent); animation: agent-tool-running 1.6s linear infinite; }
.chat-message .agent-work .agent-work-result { margin: 3px 0 0; font-size: 12px; line-height: 1.6; color: var(--muted); }
.agent-work-meta { display: flex; flex-wrap: wrap; gap: 6px 12px; margin-top: 5px; color: var(--muted); font-size: 11px; line-height: 1.6; }
.agent-work-meta code { overflow-wrap: anywhere; font-size: inherit; }
@keyframes agent-tool-running { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .agent-work-calls [data-state=running] .agent-work-icon { animation: none; } }
@media (max-width: 760px) { .chat-message .agent-work details { padding: 0 12px; } }
"""

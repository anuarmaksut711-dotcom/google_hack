"""Injectable native pairing HTTP routes and an explicit browser consent page.

W07 mounts the router and PairingPageHeaders outside existing app middleware.
No changes to WebAuth or the application factory are needed. CSP permits only
the exact inline script below and same-origin fetch; no inline handlers/forms.
"""

import base64
from functools import wraps
from hashlib import sha256
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from ..domain.models import Contract
from ..domain.ports import PortError
from ..security.context import AccessDenied
from ..voice.device_auth import DeviceAuth

PAGE_PATH = "/voice/pair"
API_PATH = "/api/voice/pairing"


PAGE_SCRIPT = r"""'use strict';
const code = document.getElementById('code');
const notice = document.getElementById('notice');
const device = document.getElementById('device');
const approve = document.getElementById('approve');
const deny = document.getElementById('deny');
const revoke = document.getElementById('revoke');
let reviewed = null;
let busy = false;
function buttons() { approve.disabled = deny.disabled = busy || reviewed === null; revoke.disabled = busy; }
async function post(action, data) {
  const csrf = document.cookie.split(';').map(x => x.trim()).filter(x => x.startsWith('__Host-cp-csrf='));
  if (csrf.length !== 1) throw new Error();
  const response = await fetch('/api/voice/pairing/' + action, {
    method: 'POST', credentials: 'same-origin', redirect: 'error',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': decodeURIComponent(csrf[0].split('=')[1])},
    body: JSON.stringify(data)
  });
  if (!response.ok) throw new Error();
  return await response.json();
}
code.addEventListener('input', () => { reviewed = null; device.textContent = ''; buttons(); });
document.getElementById('lookup').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  busy = true; reviewed = null; device.textContent = ''; buttons();
  notice.textContent = 'Проверяем код…';
  const entered = code.value.trim().toUpperCase();
  try {
    const result = await post('preview', {code: entered});
    if (entered !== code.value.trim().toUpperCase()) throw new Error();
    device.textContent = result.device_name + ' (' + (result.client_kind === 'iphone' ? 'iPhone' : 'Windows') + ') — код ' + entered;
    reviewed = entered;
    notice.textContent = 'Сверьте устройство и код с приложением. Подключить это устройство?';
  } catch (_) { notice.textContent = 'Не удалось проверить код. Войдите снова или получите новый код в приложении.'; }
  finally { busy = false; buttons(); }
});
async function decide(decision) {
  if (busy || reviewed === null || reviewed !== code.value.trim().toUpperCase()) return;
  const confirmed = reviewed;
  busy = true; buttons();
  try {
    await post('decision', {code: confirmed, decision});
    notice.textContent = decision === 'approve' ? 'Подключение разрешено. Вернитесь в приложение.' : 'Подключение отклонено.';
  } catch (_) { notice.textContent = 'Не удалось подтвердить действие. Получите новый код в приложении.'; }
  finally { reviewed = null; busy = false; buttons(); }
}
approve.addEventListener('click', () => decide('approve'));
deny.addEventListener('click', () => decide('deny'));
revoke.addEventListener('click', async () => {
  if (busy) return;
  busy = true; buttons();
  try { await post('revoke', {code: code.value.trim().toUpperCase()}); notice.textContent = 'Доступ устройства отозван.'; }
  catch (_) { notice.textContent = 'Не удалось отозвать доступ. Проверьте код и вход в аккаунт.'; }
  finally { reviewed = null; busy = false; buttons(); }
});
"""

SCRIPT_HASH = base64.b64encode(sha256(PAGE_SCRIPT.encode()).digest()).decode()
PAGE_CSP = ("default-src 'none'; script-src 'sha256-" + SCRIPT_HASH + "'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
PAGE_HTML = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Подключить устройство — CampusPulse</title>
<main><h1>Подключить голосовой помощник</h1>
<p>Устройство сможет читать задания и расписание и предлагать учебный план.
Изменения календаря требуют отдельного подтверждения в браузере.</p>
<p>Введите код только из приложения, которое вы открыли сами.
Название устройства задано приложением; сверьте также код.</p>
<form id="lookup"><label for="code">Код устройства</label>
<input id="code" name="code" maxlength="12" minlength="12" pattern="[A-HJ-NP-Z2-9]{12}"
 autocomplete="off" autocapitalize="characters" spellcheck="false" required>
<button type="submit">Показать устройство</button></form>
<p id="device"></p><p id="notice" role="status" aria-live="polite"></p>
<button id="approve" type="button" disabled>Подключить устройство</button>
<button id="deny" type="button" disabled>Отклонить подключение</button>
<p>Чтобы отключить уже подключённое устройство, введите его прежний код.</p>
<button id="revoke" type="button">Отозвать доступ устройства</button>
<noscript>Для подтверждения включите JavaScript и обновите страницу.</noscript>
</main><script>""" + PAGE_SCRIPT + "</script></html>"


class PairingPageHeaders:
    """Install OUTSIDE existing protections (last add_middleware in FastAPI).

    Existing middleware deliberately sets default-src none on every response.
    This narrowly overrides CSP on a successful pairing HTML page only; all
    other protections and paths retain the original policy. Native tokens and
    browser codes must never be logged by surrounding middleware/proxies.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def guarded(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if scope.get("path") == PAGE_PATH and message["status"] == 200:
                    headers = [(k, v) for k, v in headers if k.lower() != b"content-security-policy"]
                    headers.append((b"content-security-policy", PAGE_CSP.encode()))
                if scope.get("path") == PAGE_PATH or scope.get("path", "").startswith(API_PATH + "/"):
                    policies = {b"cache-control": b"no-store", b"referrer-policy": b"no-referrer",
                        b"x-content-type-options": b"nosniff", b"x-frame-options": b"DENY"}
                    headers = [(k, v) for k, v in headers if k.lower() not in policies]
                    headers.extend(policies.items())
                message = {**message, "headers": headers}
            await send(message)
        await self.app(scope, receive, guarded)


class StartBody(Contract):
    challenge: str
    client_kind: Literal["windows", "iphone"]
    device_name: str


class ExchangeBody(Contract):
    grant: str
    verifier: str


class CodeBody(Contract):
    code: str


class DecisionBody(CodeBody):
    decision: Literal["approve", "deny"]


async def _body(request, model):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise ValueError()
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 2048:
            raise ValueError()
    return model.model_validate_json(raw)


def _safe(function):
    @wraps(function)
    async def invoke(request: Request):
        try:

            if request.url.query:
                raise ValueError()
            return await function(request)
        except PortError:
            return JSONResponse({"error": "voice_unavailable"}, status_code=503)
        except (AccessDenied, ValueError, ValidationError):
            return JSONResponse({"error": "voice_access_denied"}, status_code=403)
    return invoke


def create_pairing_router(*, device_auth: DeviceAuth) -> APIRouter:
    """Caller injects existing WebAuth through DeviceAuth; mounts headers too."""
    service = device_auth
    router = APIRouter()

    def native(request):
        service.auth.check_request(request)

        if "cookie" in request.headers or "origin" in request.headers:
            raise AccessDenied("voice_access_denied")

    @router.get(PAGE_PATH)
    @_safe
    async def page(request: Request):
        await service.auth.load(request)
        return HTMLResponse(PAGE_HTML)

    @router.post(API_PATH + "/start")
    @_safe
    async def start(request: Request):
        native(request)
        value = await _body(request, StartBody)
        offer = await service.start(**value.model_dump(exclude={"schema_version"}))
        return JSONResponse({"pairing_code": offer.pairing_code, "grant": offer.grant,
            "expires_at": offer.expires_at.isoformat(), "approval_path": PAGE_PATH})

    @router.post(API_PATH + "/exchange")
    @_safe
    async def exchange(request: Request):
        native(request)
        value = await _body(request, ExchangeBody)
        issued = await service.exchange(**value.model_dump(exclude={"schema_version"}))
        return JSONResponse({"credential": issued.credential, "expires_at": issued.expires_at.isoformat(),
            "scope": issued.scope})

    @router.post(API_PATH + "/preview")
    @_safe
    async def preview(request: Request):
        value = await _body(request, CodeBody)
        device = await service.preview(request, value.code)
        return JSONResponse({"device_name": device.device_name, "client_kind": device.client_kind,
            "expires_at": device.expires_at.isoformat()})

    @router.post(API_PATH + "/decision")
    @_safe
    async def decision(request: Request):
        value = await _body(request, DecisionBody)
        await service.decide(request, value.code, approve=value.decision == "approve")
        return JSONResponse({"status": "approved" if value.decision == "approve" else "denied"})

    @router.post(API_PATH + "/revoke")
    @_safe
    async def revoke(request: Request):
        value = await _body(request, CodeBody)
        await service.revoke(request, value.code)
        return JSONResponse({"status": "revoked"})

    return router

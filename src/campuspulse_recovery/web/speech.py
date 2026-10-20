"""Bounded authenticated audio upload; recordings are never persisted."""
import asyncio
import json

from fastapi import APIRouter, Request

from ..security.context import AccessDenied
from .app import _json

MAX_AUDIO_BYTES = 4 * 1024 * 1024
AUDIO_TYPES = {'audio/webm', 'audio/mp4', 'audio/ogg', 'audio/wav'}


def create_speech_router(*, auth, agent):
    router = APIRouter()
    lock = asyncio.Lock()

    @router.post('/api/chat/transcribe')
    async def transcribe(request: Request):
        credential = await auth.load(request, mutation=True)
        if not callable(getattr(agent, 'transcribe', None)):
            return _json({'error': 'speech_not_configured'}, 503)
        mime = request.headers.get('content-type', '').split(';', 1)[0].lower()
        if mime not in AUDIO_TYPES:
            return _json({'error': 'unsupported_audio'}, 415)
        language = request.headers.get('x-speech-language', 'ru-RU')
        if language not in {'ru-RU', 'kk-KZ', 'auto'}:
            return _json({'error': 'unsupported_language'}, 400)
        if lock.locked():
            return _json({'error': 'speech_busy'}, 429)
        async with lock:
            audio = bytearray()
            async for chunk in request.stream():
                audio.extend(chunk)
                if len(audio) > MAX_AUDIO_BYTES:
                    return _json({'error': 'audio_too_large'}, 413)
            if not audio:
                return _json({'error': 'empty_audio'}, 400)
            try:
                async with asyncio.timeout(55):
                    text = await agent.transcribe(audio=bytes(audio), mime_type=mime, language=language)
            except Exception as error:
                # No audio, transcript, credentials or raw provider messages in logs.
                code = getattr(error, 'code', None)
                print(json.dumps({'event': 'speech_failed', 'type': type(error).__name__,
                    'code': code if isinstance(code, int) else None, 'mime_type': mime,
                    'audio_bytes': len(audio)}), flush=True)
                return _json({'error': 'transcription_failed'}, 503)
            latest = await auth.load(request)
            if latest.session != credential.session:
                raise AccessDenied('speech_session_changed')
            if not isinstance(text, str) or len(text) > 4000:
                return _json({'error': 'transcription_failed'}, 503)
            return _json({'text': text.strip()})

    return router

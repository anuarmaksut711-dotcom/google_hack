"""W04 managed provider + read handler, using actual Vertex 1.165.1 bidi API.

No model runs in the backend. execute() sends only an exact pending call ID;
runtime executes MCP and returns an opaque receipt, never source evidence.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from pydantic import TypeAdapter

from ..security.context import AccessDenied
from ..voice import protocol as p
from ..voice.ports import ProviderMode, ToolRequest, ToolResult
from ..web.agent_client import AgentEngineTarget
from .voice_runtime import (CLOSE_SECONDS, Receipt, bounded, decode_envelope,
    decode_event, encode_input, envelope, exact, private_logs)


class ManagedVoiceProvider:
    mode = ProviderMode.MANAGED_PRODUCTION

    def __init__(self, target, *, issue_reference, authorize, credentials_provider,
                 client_factory=None):
        if any(x is None for x in (target, issue_reference, authorize, credentials_provider)):
            raise ValueError("managed_voice_backend_binding_required")
        self.target = AgentEngineTarget.model_validate(target)


        self.issue_reference, self.authorize = issue_reference, authorize
        self.credentials_provider, self.client_factory = credentials_provider, client_factory
        self.session = None
        self.used = False

    async def open(self, lease, resume):
        if self.used:
            raise ValueError("managed_voice_provider_single_use")
        self.used = True
        session = ManagedVoiceSession(self, lease)
        try:


            with private_logs():
                async with asyncio.timeout(5):
                    await session.start()
            self.session = session
            return session
        except BaseException as error:
            try:
                await session.close()
            except Exception:
                if not isinstance(error, asyncio.CancelledError):
                    raise ValueError("managed_voice_close_failed") from None
            if isinstance(error, (asyncio.CancelledError, AccessDenied)):
                raise
            raise ValueError("managed_voice_unavailable") from None

    async def execute(self, access, request):
        if self.session is None:
            raise AccessDenied("managed_voice_session_required")
        return await self.session.execute(access, request)


class ManagedVoiceSession:
    def __init__(self, provider, lease):
        self.provider, self.lease = provider, lease
        self.client = self.manager = self.connection = self.reader = None
        self.events = asyncio.Queue(maxsize=32)
        self.pending = {}
        self.receipts = {}
        self.write_lock = asyncio.Lock()
        self.seq = self.received_seq = 0
        self.closed = False

    async def check(self):
        access = await bounded(self.provider.authorize(self.lease), 2)
        if (access.binding != self.lease.binding or access.device_id != self.lease.device_id):
            raise AccessDenied("managed_voice_backend_binding_rejected")
        return access

    async def start(self):
        await self.check()
        reference = await bounded(self.provider.issue_reference(self.lease), 2)
        credentials = await bounded(self.provider.credentials_provider(), 5)


        expiry = getattr(credentials, "expiry", None)
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if (not getattr(credentials, "valid", False) or not getattr(credentials, "token", None)
                or expiry is None or expiry <= datetime.now(UTC) + timedelta(seconds=600)):
            raise AccessDenied("managed_voice_fresh_platform_credentials_required")
        factory = self.provider.client_factory
        if factory is None:
            import vertexai
            factory = vertexai.Client
        with private_logs():
            self.client = factory(project=self.provider.target.project_id,
                location=self.provider.target.location, credentials=credentials,
                http_options={"timeout": 5000, "retry_options": {"attempts": 1}})
            self.manager = self.client.aio.live.agent_engines.connect(
                agent_engine=self.provider.target.resource_name,
                config={"class_method": "bidi_stream_query"})
        self.connection = await bounded(self.manager.__aenter__(), 5)
        await self._send("open", {"reference": reference})
        first = await self._receive(5)
        exact(first.body, "connection_generation")
        TypeAdapter(p.PositiveU32).validate_python(first.body["connection_generation"])
        if first.kind != "ready" or first.body["connection_generation"] != self.lease.connection_generation:
            raise ValueError("managed_voice_ready_rejected")
        with private_logs():
            self.reader = asyncio.create_task(self._pump(), name="managed_voice_receiver")

    async def _send(self, kind, body):
        async with self.write_lock:
            if self.closed:
                raise ValueError("managed_voice_closed")
            await self.check()
            message = envelope(self.seq, kind, body)
            await bounded(self.connection.send(query_input=message))
            self.seq += 1

    async def _receive(self, seconds=60):
        message = decode_envelope(await bounded(self.connection.receive(), seconds))
        await self.check()
        if message.seq != self.received_seq:
            raise ValueError("managed_voice_sequence_rejected")
        self.received_seq += 1
        return message

    async def _pump(self):
        try:
            while True:
                message = await self._receive()
                if message.kind == "event":
                    event = decode_event(message.body)
                    if isinstance(event, ToolRequest):
                        if event.call_id in self.pending or len(self.pending) >= 24:
                            raise ValueError("managed_voice_call_replay")
                        self.pending[event.call_id] = (event, "requested")
                    await bounded(self.events.put(event))
                elif message.kind == "receipt":
                    exact(message.body, "call_id", "status", "receipt")
                    call_id = message.body["call_id"]
                    future = self.receipts.get(call_id)
                    if (future is None or future.done() or self.pending[call_id][1] != "executing"
                            or message.body["status"] not in ("success", "failed", "unavailable")):
                        raise AccessDenied("managed_voice_receipt_rejected")
                    TypeAdapter(Receipt).validate_python(message.body["receipt"])
                    result = ToolResult(message.body["status"], {"receipt": message.body["receipt"]})
                    self.pending[call_id] = (self.pending[call_id][0], result)
                    future.set_result(result)
                elif message.kind == "closed":
                    exact(message.body)
                    return
                else:
                    raise ValueError("managed_voice_upstream_unavailable")
        finally:
            for future in self.receipts.values():
                if not future.done():
                    future.cancel()

    async def send(self, packet):
        await self._send("input", encode_input(packet))

    async def receive(self):
        while True:
            if self.reader.done():
                await self.reader
                return None
            reader = asyncio.create_task(self.events.get())
            try:
                done, _ = await asyncio.wait([reader, self.reader], return_when=asyncio.FIRST_COMPLETED)
                if self.reader in done:
                    await self.reader
                    return None
                await self.check()
                return reader.result()
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    async def execute(self, access, request):
        fresh = await self.check()
        entry = self.pending.get(request.call_id)
        if fresh != access or entry != (request, "requested"):
            raise AccessDenied("managed_voice_dispatch_rejected")


        self.pending[request.call_id] = (request, "executing")
        future = asyncio.get_running_loop().create_future()
        self.receipts[request.call_id] = future
        try:
            await self._send("dispatch", {"call_id": request.call_id})
            return await bounded(future)
        finally:
            self.receipts.pop(request.call_id, None)

    async def tool_result(self, call_id, result):
        entry = self.pending.get(call_id)
        if entry is None or not isinstance(entry[1], ToolResult) or entry[1] != result:
            raise AccessDenied("managed_voice_result_rejected")
        self.pending[call_id] = (entry[0], "finished")
        await self._send("result_ack", {"call_id": call_id, "status": result.status,
                                      "receipt": result.value["receipt"]})

    async def cancel(self):

        try:
            with private_logs():
                async with asyncio.timeout(CLOSE_SECONDS):
                    if self.reader is not None:
                        self.reader.cancel()
                        await asyncio.gather(self.reader, return_exceptions=True)
                    if self.connection is not None and not self.closed:
                        await self.connection.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ValueError("managed_voice_unavailable") from None

    async def close(self):
        if self.closed:
            return
        self.closed = True
        deadline = asyncio.get_running_loop().time() + CLOSE_SECONDS
        error = None
        owned = []



        closers = ([] if self.connection is None else [self.connection.close])
        if self.manager is not None:
            closers.append(lambda: self.manager.__aexit__(None, None, None))
        if self.client is not None:
            closers.append(self.client.aio.aclose)
        if self.reader is not None:
            self.reader.cancel()
        async def close_one(action):
            with private_logs():
                return await action()
        try:
            owned = [asyncio.create_task(close_one(action)) for action in closers]
            async with asyncio.timeout_at(deadline):
                results = await asyncio.gather(*owned, return_exceptions=True)
                if any(isinstance(result, BaseException) for result in results):
                    error = ValueError("managed_voice_close_failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            error = ValueError("managed_voice_unavailable")
        finally:


            for task in owned:
                if not task.done():
                    task.cancel()
            joining = owned + ([] if self.reader is None else [self.reader])
            try:
                if joining:
                    await asyncio.gather(*joining, return_exceptions=True)
            finally:
                if self.client is not None:


                    try:
                        with private_logs():
                            self.client._api_client.close()
                    except Exception:
                        error = error or ValueError("managed_voice_close_failed")
                self.pending.clear()
                self.receipts.clear()
                while not self.events.empty():
                    self.events.get_nowait()
        if error is not None:
            raise error

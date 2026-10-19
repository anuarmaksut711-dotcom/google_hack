"""Demo-only document and Workspace edges. No Google SDK or credentials.

Production repositories/readers/writer run unchanged above these adapters.
The process lock is test-store synchronization, not deployed Calendar locking.
"""
import asyncio
from copy import deepcopy
from datetime import timedelta
from threading import RLock
from types import SimpleNamespace

from ..config import AppConfig
from ..domain.ports import BindingConflict
from ..integrations.assignments import HEADERS
from ..integrations.google_credentials import WorkspaceAccessError
from ..security.context import AccessDenied
from . import require_demo


def demo_config():
    # Deliberately fictitious identifiers/locations, never a cloud configuration.
    return AppConfig.model_validate(dict(mode="local_development", project_id="campuspulse-demo",
        agent_region="demo-region1", model_location="demo-region1",
        web_cloud_run_region="demo-region1", mcp_cloud_run_region="demo-region1",
        firestore_location="demo-region1", firestore_database="(default)",
        workspace_access_mode="explicit_resource_sharing", student=dict(
            trusted_user_id="demo-student", allowed_signin_principal="https://demo.invalid#student",
            spreadsheet_id="demo-assignments", assignments_a1_range="'Assignments'!A1:F100",
            calendar_id="demo-calendar", timezone="Asia/Qyzylorda"),
        identities={role+"_service_account":"demo-"+role+"@campuspulse-demo.iam.gserviceaccount.com"
                    for role in ("backend", "agent", "mcp")}, secrets={}))


class MemoryDocuments:
    def __init__(self, config, *, demo):
        require_demo(demo)
        self.project_id, self.database_id = config.project_id, config.firestore_database
        self.data, self.lock = {}, asyncio.Lock()

    async def get(self, path):
        return deepcopy(self.data.get(path))

    async def scan(self, collection):
        return tuple(deepcopy(v) for p,v in self.data.items() if p.rsplit("/",1)[0] == collection)

    async def transact(self, callback):
        async with self.lock:
            pending, wrote = deepcopy(self.data), False
            async def get(path):
                if wrote:
                    raise BindingConflict("demo_transaction_reads_after_writes")
                return deepcopy(pending.get(path))
            def create(path, value):
                nonlocal wrote
                if path in pending:
                    raise BindingConflict("demo_document_already_exists")
                wrote = True
                pending[path] = deepcopy(value)
            def put(path, value):
                nonlocal wrote
                wrote = True
                pending[path] = deepcopy(value)
            result = await callback(SimpleNamespace(get=get,create=create,set=put))
            self.data = pending
            return result


class _Request:
    def __init__(self, operation, options):
        self.operation, self.options, self.headers = operation, options, {}


class _Events:
    def events(self):
        return self

    def __getattr__(self, operation):
        if operation not in ("list", "get", "insert", "patch"):
            raise AttributeError(operation)
        return lambda **options: _Request(operation, options)


class DemoWorkspace:
    def __init__(self, policy, now, *, demo):
        require_demo(demo)
        self.policy, self.read_only = policy, False
        self.lock, self.reads, self.writes = RLock(), [], []
        # Relative to every fresh startup, on the solver's 15-minute grid.
        self.anchor = now.replace(second=0,microsecond=0) + timedelta(minutes=30-now.minute%15)
        self.rows = [["ml", "ML", "Лабораторная работа", (self.anchor+timedelta(hours=7)).isoformat(),120,"todo"],
                     ["db", "Базы данных", "Отчёт", (self.anchor+timedelta(hours=5)).isoformat(),60,"todo"]]
        self.events = {"demo-class":dict(id="demo-class",etag='"demo-1"',status="confirmed",
            transparency="opaque",start={"dateTime":(self.anchor+timedelta(hours=1)).isoformat()},
            end={"dateTime":(self.anchor+timedelta(hours=2)).isoformat()})}

    def sheet_read(self, context, *, spreadsheet_id, a1_range, request_factory):
        self.policy.require_sheet(context,spreadsheet_id,a1_range)
        if context.boundary != "mcp":
            raise AccessDenied("mcp_boundary_required")
        with self.lock:
            self.reads.append(("assignments",context.trusted.run_id))
            return {"majorDimension":"ROWS","values":[list(HEADERS),*deepcopy(self.rows)]}

    def calendar_request(self, context, *, calendar_id, request_factory, mutation=False):
        self.policy.require_calendar(context,calendar_id)
        if context.boundary != "mcp":
            raise AccessDenied("mcp_boundary_required")
        request = request_factory(_Events(),calendar_id)
        op, args = request.operation, request.options
        if args.get("calendarId") != calendar_id or (op in ("insert","patch") and not mutation):
            raise AccessDenied("demo_calendar_request_rejected")
        with self.lock:
            if op == "list":
                self.reads.append(("calendar",context.trusted.run_id))
                return {"timeZone":context.trusted.timezone,"items":deepcopy(list(self.events.values()))}
            identity = args.get("eventId")
            if op == "get":
                self.reads.append(("event",context.trusted.run_id))
                if identity not in self.events:
                    raise WorkspaceAccessError("demo_event_missing",http_status=404)
                return deepcopy(self.events[identity])
            body = deepcopy(args["body"])
            if op == "insert":
                identity = body["id"]
                if identity in self.events:
                    raise WorkspaceAccessError("demo_event_collision",http_status=409)
                saved = body
            elif op == "patch":
                old = self.events.get(identity)
                if old is None or request.headers.get("If-Match") != old["etag"]:
                    raise WorkspaceAccessError("demo_stale_event",http_status=412)
                saved = {**old,**body}
            else:
                raise AccessDenied("demo_calendar_operation_rejected")
            saved["etag"] = '"demo-'+str(len(self.writes)+2)+'"'
            self.events[identity] = saved
            self.writes.append(dict(operation=op,event_id=identity,run_id=context.trusted.run_id))
            return deepcopy(saved)


class NoUncertainTakeover:
    async def require_stopped(self, context, transaction, previous, now):
        # No invented stop certificate. Unknown writers remain blocked.
        raise BindingConflict("demo_restart_required_for_uncertain_writer")

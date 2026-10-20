"""User-scoped chat catalog; conversation contents stay in the existing store."""
from typing import Annotated, Literal

from fastapi import APIRouter, Request
from pydantic import Field, StringConstraints, TypeAdapter, field_validator, model_validator

from ..domain.models import Contract, Instant, canonical_hash
from ..domain.ports import BindingConflict
from ..observability.events import TechnicalId
from ..security.context import AccessDenied
from ..storage.plans import _Repository, _decode, _encode, _key
from .app import body, _json

ChatId = Annotated[str, StringConstraints(strict=True, pattern=r'^(default|[a-f0-9]{32})$')]
FolderId = Annotated[str, StringConstraints(strict=True, pattern=r'^[a-f0-9]{32}$')]
Title = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80)]
Revision = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class LegacyChatEntry(Contract):
    chat_id: ChatId
    title: Title
    folder_id: FolderId | None = None
    creation_digest: str | None = None


class Version2ChatEntry(LegacyChatEntry):
    updated_at: Instant | None = None
    auto_title: bool = False


class ChatEntry(Version2ChatEntry):
    manual_title: bool = False


class FolderEntry(Contract):
    folder_id: FolderId
    title: Title
    creation_digest: str | None = None


class Receipt(Contract):
    request_key: TechnicalId
    digest: str
    item_id: str


class LegacyChatWorkspace(Contract):
    revision: Revision = 0
    chats: Annotated[tuple[LegacyChatEntry, ...], Field(max_length=120)] = (
        LegacyChatEntry(chat_id='default', title='Первый чат'),)
    folders: Annotated[tuple[FolderEntry, ...], Field(max_length=40)] = ()
    receipts: Annotated[tuple[Receipt, ...], Field(max_length=128)] = ()


class Version2ChatWorkspace(Contract):
    revision: Revision = 0
    chats: Annotated[tuple[Version2ChatEntry, ...], Field(max_length=120)] = (
        Version2ChatEntry(chat_id='default', title='Первый чат', auto_title=True),)
    folders: Annotated[tuple[FolderEntry, ...], Field(max_length=40)] = ()
    receipts: Annotated[tuple[Receipt, ...], Field(max_length=128)] = ()


class ChatWorkspace(Version2ChatWorkspace):
    chats: Annotated[tuple[ChatEntry, ...], Field(max_length=120)] = (
        ChatEntry(chat_id='default', title='Первый чат', auto_title=True),)


class WorkspaceChange(Contract):
    action: Literal['create_chat', 'create_folder', 'rename_chat', 'rename_folder', 'move_chat',
                    'delete_chat', 'delete_folder']
    request_key: TechnicalId
    revision: Revision
    item_id: ChatId | None = None
    title: Title | None = None
    folder_id: FolderId | None = None

    @field_validator('title')
    @classmethod
    def clean_title(cls, value):
        if value is not None:
            value = value.strip()
            if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError('invalid_title')
        return value

    @model_validator(mode='after')
    def fields_for_action(self):
        required = {
            'create_chat': {'title', 'folder_id'}, 'create_folder': {'title'},
            'rename_chat': {'item_id', 'title'}, 'rename_folder': {'item_id', 'title'},
            'move_chat': {'item_id', 'folder_id'},
            'delete_chat': {'item_id'}, 'delete_folder': {'item_id'},
        }[self.action]
        if self.model_fields_set != required | {'action', 'request_key', 'revision'}:
            raise ValueError('invalid_workspace_change')
        if 'title' in required and self.title is None or 'item_id' in required and self.item_id is None:
            raise ValueError('missing_workspace_value')
        return self


class WorkspaceStore(_Repository):
    def __init__(self, *args, owner_history=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner_history = owner_history

    def scope_key(self, context):
        c = self._request(context)
        return _key(c.trusted.project_id, c.trusted.owner_id, c.trusted.calendar_id,
                    c.trusted.config_revision, 'owner-history-v1' if self.owner_history else c.session_id)

    def path(self, context):
        return 'cp_chat_workspaces/' + self.scope_key(context)

    def conversation_path(self, context, chat_id):
        TypeAdapter(ChatId).validate_python(chat_id)
        scope = self.scope_key(context)
        return 'cp_conversations/' + (scope if chat_id == 'default' else _key(scope, chat_id))

    def deleted_item_path(self, context, item_id):
        return 'cp_chat_deleted_items/' + _key(self.scope_key(context), item_id)

    @staticmethod
    def decode(saved):
        if saved is None:
            return ChatWorkspace()
        if saved.get('kind') == 'chat-workspace-v1':
            legacy = _decode(saved, 'chat-workspace-v1', LegacyChatWorkspace)
            return ChatWorkspace.model_validate(legacy.model_dump())
        if saved.get('kind') == 'chat-workspace-v2':
            try:
                previous = _decode(saved, 'chat-workspace-v2', Version2ChatWorkspace)
            except BindingConflict:
                # Also read the short-lived v2 writer that included manual_title.
                # Both shapes still require their original exact bytes and digest.
                return _decode(saved, 'chat-workspace-v2', ChatWorkspace)
            return ChatWorkspace.model_validate(previous.model_dump())
        return _decode(saved, 'chat-workspace-v3', ChatWorkspace)

    async def read(self, context):
        saved = await self.documents.get(self.path(context))
        return self.decode(saved)

    @staticmethod
    def automatic_title(chat):
        return not chat.manual_title and (chat.auto_title or chat.title in ('Новый чат', 'Первый чат'))

    async def needs_title(self, context, chat_id):
        return any(chat.chat_id == chat_id and self.automatic_title(chat) for chat in (await self.read(context)).chats)

    async def record_activity(self, context, chat_id, message, now, *, title=None):
        path = self.path(context)
        async def transact(tx):
            current = self.decode(await tx.get(path))
            chats = list(current.chats)
            index = next((i for i, chat in enumerate(chats) if chat.chat_id == chat_id), None)
            if index is None:
                raise AccessDenied('chat_not_available')
            chat = chats[index]
            fallback = ' '.join(message.split())
            fallback = ''.join(c for c in fallback if ord(c) >= 32 and ord(c) != 127)[:80]
            automatic = self.automatic_title(chat) or (title is not None and not chat.manual_title and chat.title == fallback)
            if chat.updated_at is not None and chat.updated_at >= now and not automatic:
                return
            update = {'updated_at': max(chat.updated_at, now) if chat.updated_at else now}
            if automatic:
                suggested = ' '.join(title.split()).strip('"«»“”') if isinstance(title, str) else ''
                valid = 0 < len(suggested) <= 80 and all(ord(c) >= 32 and ord(c) != 127 for c in suggested)
                update.update(title=(suggested if valid else fallback) or chat.title, auto_title=False)
            chats[index] = chat.model_copy(update=update)
            result = current.model_copy(update={'chats': tuple(chats), 'revision': current.revision + 1})
            tx.set(path, _encode('chat-workspace-v3', result))
        await self.documents.transact(transact)

    async def require_chat(self, context, chat_id, *, reader=None):
        current = self.decode(await (reader or self.documents).get(self.path(context)))
        if not any(c.chat_id == chat_id for c in current.chats):
            raise AccessDenied('chat_not_available')

    async def change(self, context, value, *, now=None):
        path = self.path(context)
        # Stable across retries, including a lost successful HTTP response.
        new_id = _key(self.scope_key(context), value.request_key)[:32]
        digest = canonical_hash(value.model_dump(exclude={'revision'}))

        async def transact(tx):
            saved = await tx.get(path)
            current = self.decode(saved)
            if value.action in ('create_chat', 'create_folder'):
                # A delayed creation retry must never resurrect a deleted item,
                # even after its original receipt has left the bounded catalog.
                if await tx.get(self.deleted_item_path(context, new_id)) is not None:
                    raise AccessDenied('workspace_item_deleted')
            previous = next((r for r in current.receipts if r.request_key == value.request_key), None)
            if previous:
                if previous.digest != digest:
                    raise BindingConflict('workspace_request_changed')
                return current, previous.item_id
            if value.action in ('create_chat', 'create_folder'):
                existing = next((item for item in (*current.chats, *current.folders)
                    if getattr(item, 'chat_id', getattr(item, 'folder_id', None)) == new_id), None)
                if existing is not None:
                    if existing.creation_digest != digest:
                        raise BindingConflict('workspace_request_changed')
                    return current, new_id
            if value.revision != current.revision:
                raise BindingConflict('workspace_revision_changed')
            chats, folders = list(current.chats), list(current.folders)
            folder_id = value.folder_id
            if folder_id is not None and not any(f.folder_id == folder_id for f in folders):
                if value.action != 'create_chat':
                    raise AccessDenied('folder_not_available')
                deleted = await tx.get(self.deleted_item_path(context, folder_id))
                if deleted is None or deleted.get('kind') != 'deleted-chat-project-v1':
                    raise AccessDenied('folder_not_available')
                _decode(deleted, 'deleted-chat-project-v1', Contract)
                # A first-send retry may outlive its project. Preserve its
                # original fingerprint/key, but save the chat in the general list.
                folder_id = None
            item_id = value.item_id or new_id
            if value.action == 'create_chat':
                if len(chats) >= 120:
                    raise BindingConflict('chat_count_limit')
                chats.append(ChatEntry(chat_id=item_id, title=value.title, folder_id=folder_id,
                    creation_digest=digest, auto_title=value.title == 'Новый чат', updated_at=now))
            elif value.action == 'create_folder':
                if len(folders) >= 40:
                    raise BindingConflict('folder_count_limit')
                folders.append(FolderEntry(folder_id=item_id, title=value.title, creation_digest=digest))
            elif value.action in ('rename_chat', 'move_chat', 'delete_chat'):
                index = next((i for i, c in enumerate(chats) if c.chat_id == item_id), None)
                if index is None:
                    raise AccessDenied('chat_not_available')
                if value.action == 'delete_chat':
                    chats.pop(index)
                    marker = _encode('deleted-chat-item-v1', Contract())
                    # Replace the live history with a content-free marker in the
                    # same transaction that removes catalog access.
                    tx.set(self.conversation_path(context, item_id), marker)
                    tx.set(self.deleted_item_path(context, item_id), marker)
                else:
                    update = {'title': value.title, 'auto_title': False, 'manual_title': True} if value.action == 'rename_chat' else {'folder_id': value.folder_id}
                    chats[index] = chats[index].model_copy(update=update)
            else:
                index = next((i for i, f in enumerate(folders) if f.folder_id == item_id), None)
                if index is None:
                    raise AccessDenied('folder_not_available')
                if value.action == 'delete_folder':
                    folders.pop(index)
                    chats = [chat.model_copy(update={'folder_id': None}) if chat.folder_id == item_id else chat for chat in chats]
                    tx.set(self.deleted_item_path(context, item_id), _encode('deleted-chat-project-v1', Contract()))
                else:
                    folders[index] = folders[index].model_copy(update={'title': value.title})
            result = ChatWorkspace(revision=current.revision + 1, chats=tuple(chats), folders=tuple(folders),
                receipts=(*current.receipts[-127:], Receipt(request_key=value.request_key, digest=digest, item_id=item_id)))
            tx.set(path, _encode('chat-workspace-v3', result))
            return result, item_id

        return await self.documents.transact(transact)


def create_workspace_router(*, auth, documents, policy, owner_history=False, chat_available=False, speech_available=False):
    router = APIRouter()
    store = WorkspaceStore(documents, policy, owner_history=owner_history)

    async def fresh(request, credential):
        if (await auth.load(request)).session != credential.session:
            raise AccessDenied('workspace_session_changed')

    def view(workspace):
        return {**workspace.model_dump(mode='json', exclude={'receipts': True,
            'chats': {'__all__': {'creation_digest', 'auto_title', 'manual_title'}}, 'folders': {'__all__': {'creation_digest'}}}),
            'chat_available': chat_available, 'speech_available': speech_available}

    @router.get('/api/chat/workspace')
    async def read(request: Request):
        credential = await auth.load(request)
        if request.query_params:
            raise ValueError('workspace_selectors_forbidden')
        with auth.request_scope(credential) as context:
            result = await store.read(context)
            await fresh(request, credential)
            return _json(view(result))

    @router.post('/api/chat/workspace')
    async def change(request: Request):
        credential = await auth.load(request, mutation=True)
        if request.query_params:
            raise ValueError('workspace_selectors_forbidden')
        value = await body(request, WorkspaceChange)
        with auth.request_scope(credential) as context:
            result, item_id = await store.change(context, value, now=auth.now())
            await fresh(request, credential)
            return _json({**view(result), 'item_id': item_id})

    return router

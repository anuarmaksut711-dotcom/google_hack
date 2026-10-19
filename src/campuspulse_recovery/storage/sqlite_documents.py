"""Durable document port for the local multi-user server; encrypted at rest."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sqlite3

from cryptography.fernet import Fernet

from ..domain.ports import BindingConflict


class SqliteDocuments:
    def __init__(self, directory: Path, *, project_id, database_id):
        directory.mkdir(parents=True, exist_ok=True)
        key_path = directory / 'storage.key'
        try:
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes())
        self.path = directory / 'documents.sqlite3'
        self.project_id, self.database_id = project_id, database_id
        self.lock = asyncio.Lock()
        with self.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS documents (path TEXT PRIMARY KEY, value BLOB NOT NULL)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def encode(self, value):
        def binary(item):
            if isinstance(item, bytes):
                return {'__bytes__': base64.b64encode(item).decode('ascii')}
            raise TypeError('unsupported_document_value')
        raw = json.dumps(value, default=binary, ensure_ascii=False).encode('utf-8')
        if len(raw) > 2_000_000:
            raise ValueError('document_too_large')
        return self.cipher.encrypt(raw)

    def decode(self, raw):
        def binary(item):
            return base64.b64decode(item['__bytes__'], validate=True) if set(item) == {'__bytes__'} else item
        return json.loads(self.cipher.decrypt(raw), object_hook=binary)

    async def get(self, path):
        with self.connect() as connection:
            row = connection.execute('SELECT value FROM documents WHERE path=?', (path,)).fetchone()
        return None if row is None else self.decode(row[0])

    async def scan(self, collection):
        with self.connect() as connection:
            rows = connection.execute('SELECT path,value FROM documents').fetchall()
        return tuple(self.decode(value) for path, value in rows if path.rsplit('/', 1)[0] == collection)

    async def transact(self, callback):
        async with self.lock:
            connection = self.connect()
            store = self

            class Transaction:
                async def get(self, path):
                    row = connection.execute('SELECT value FROM documents WHERE path=?', (path,)).fetchone()
                    return None if row is None else store.decode(row[0])

                def create(self, path, value):
                    try:
                        connection.execute('INSERT INTO documents VALUES (?,?)', (path, store.encode(value)))
                    except sqlite3.IntegrityError:
                        raise BindingConflict('document_already_exists') from None

                def set(self, path, value):
                    connection.execute('INSERT INTO documents VALUES (?,?) ON CONFLICT(path) DO UPDATE SET value=excluded.value',
                                       (path, store.encode(value)))

            try:
                connection.execute('BEGIN IMMEDIATE')
                result = await callback(Transaction())
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

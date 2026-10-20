"""Offline construction check, with synthetic credentials and no dispatched I/O."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / 'src'), str(root)]
from tests.lms.test_settings import valid_payload
from campuspulse_recovery.cloud.settings import CloudSettings
from campuspulse_recovery.cloud.mcp import create_mcp
from campuspulse_recovery.security.resources import ResourcePolicy
from campuspulse_recovery.storage.plans import FirestoreDocuments
from google.cloud.firestore import AsyncClient
from google.oauth2.credentials import Credentials

raw = valid_payload()
raw['application']['student']['assignments_a1_range'] = 'Assignments!A1:F100'
settings = CloudSettings.model_validate(raw)
policy = ResourcePolicy(settings.application, environment='cloud')
client = AsyncClient(project=settings.application.project_id,
    database=settings.application.firestore_database, credentials=Credentials('synthetic-no-network'))
documents = FirestoreDocuments(client, project_id=settings.application.project_id,
    database_id=settings.application.firestore_database)
app = create_mcp(settings, policy, documents, port=8080, close=lambda: None)
assert callable(app)
print('offline_mcp_composition_ok; no tools dispatched; authentication unchanged')

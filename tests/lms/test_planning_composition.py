"""Real production service construction, without credentials or HTTP requests."""
from campuspulse_recovery.cloud.planning import assignments_resource
from campuspulse_recovery.cloud.mcp import create_mcp
from campuspulse_recovery.lms.planning import MoodleAssignmentsReader
from campuspulse_recovery.security.resources import ResourcePolicy
from tests.lms.test_settings import valid_payload, validate
from tests.storage.test_plans import FakeDocuments


def test_complete_mcp_selects_moodle_reader_and_retains_domain_guards(monkeypatch):
    import campuspulse_recovery.cloud.mcp as composition
    settings = validate(valid_payload())
    policy = ResourcePolicy(settings.application, environment='cloud',
        assignments_resource_id=assignments_resource(settings))
    captured = {}
    original = composition.create_complete_app
    def create(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(composition, 'create_complete_app', create)
    app = create_mcp(settings, policy, FakeDocuments(), port=8080, close=lambda: None)
    assert callable(app)
    assert isinstance(captured['assignments'], MoodleAssignmentsReader)
    assert captured['apply'].preflight.assignments is captured['assignments']
    assert captured['verification'].assignments is captured['assignments']


def test_sheet_deployment_keeps_original_reader(monkeypatch):
    import campuspulse_recovery.cloud.mcp as composition
    data = valid_payload()
    del data['moodle']
    data['application']['student']['assignments_a1_range'] = 'Tasks!A1:F100'
    settings = validate(data)
    policy = ResourcePolicy(settings.application, environment='cloud')
    captured = {}
    original = composition.create_complete_app
    def create(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(composition, 'create_complete_app', create)
    create_mcp(settings, policy, FakeDocuments(), port=8080, close=lambda: None)
    assert not isinstance(captured['assignments'], MoodleAssignmentsReader)

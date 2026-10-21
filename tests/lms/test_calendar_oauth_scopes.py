"""Exercise the installed OAuth libraries' real scope-response parser offline."""
import json

import pytest
import requests
from google_auth_oauthlib.flow import Flow

from campuspulse_recovery.integrations.local_calendar import (
    CalendarConnectionError, SCOPES, fetch_google_token)
from tests.lms.test_local_google_calendar import config, ORIGIN, CALLBACK


@pytest.mark.parametrize('granted,accepted', [
    (list(SCOPES), True),
    (list(reversed(SCOPES)), True),
    ([*SCOPES, 'https://www.googleapis.com/auth/userinfo.email',
      'https://www.googleapis.com/auth/userinfo.profile'], True),
    (['openid'], False),
    (['https://www.googleapis.com/auth/userinfo.email'], False),
])
def test_google_reconnect_superset_keeps_one_exchange_and_missing_scopes_fail(monkeypatch, granted, accepted):
    flow = Flow.from_client_config(config(), scopes=SCOPES, redirect_uri=ORIGIN + CALLBACK,
        code_verifier='test-pkce-verifier')
    calls = []
    def response(*args, **kwargs):
        calls.append((args, kwargs))
        result = requests.Response()
        result.status_code = 200
        result.url = 'https://oauth2.googleapis.com/token'
        result.request = requests.Request('POST', result.url).prepare()
        result._content = json.dumps({'access_token': 'synthetic-access-token', 'refresh_token': 'synthetic-refresh-token',
            'token_type': 'Bearer', 'expires_in': 3600, 'id_token': 'synthetic-id-token',
            'scope': ' '.join(granted)}).encode()
        return result
    monkeypatch.delenv('OAUTHLIB_RELAX_TOKEN_SCOPE', raising=False)
    monkeypatch.setattr(flow.oauth2session, 'request', response)
    if accepted:
        fetch_google_token(flow, code='synthetic-code', scopes=SCOPES)
        assert flow.credentials.has_scopes(SCOPES)
        assert flow.credentials.refresh_token == 'synthetic-refresh-token'
        assert set(flow.credentials.granted_scopes) == set(granted)
    else:
        with pytest.raises(CalendarConnectionError):
            fetch_google_token(flow, code='synthetic-code', scopes=SCOPES)
    assert len(calls) == 1
    assert 'OAUTHLIB_RELAX_TOKEN_SCOPE' not in __import__('os').environ

import asyncio
import json

import httpx
import pytest

from campuspulse_recovery.lms.github_repository import (
    GitHubRepositorySnapshot, MAX_RESPONSE_BYTES, extract_github_repository,
    normalize_github_repository, read_github_repository,
)


REPOSITORY = 'https://github.com/student/lab-work'


def read(handler, repository=REPOSITORY):
    return asyncio.run(read_github_repository(repository, transport=httpx.MockTransport(handler)))


def issue(number=1, **extra):
    return {'number': number, 'title': 'Написать тесты', 'body': 'Проверить алгоритм', **extra}


def test_real_read_routes_public_data_and_filters_pull_requests_without_credentials():
    calls = []
    def provider(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.url.host == 'api.github.com'
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        assert request.url.path.startswith('/repos/student/lab-work/')
        if request.url.path.endswith('/readme'):
            assert request.headers['accept'] == 'application/vnd.github.raw+json'
            return httpx.Response(200, text='# Лабораторная\nРеализуйте сортировку.\x00')
        assert dict(request.url.params) == {'state': 'open', 'per_page': '30'}
        return httpx.Response(200, json=[issue(), issue(2, pull_request={'url': 'private'}),
            issue(3, html_url='https://evil.example/phishing', user={'login': 'not-needed'})])

    result = read(provider)
    assert len(calls) == 2
    assert result.status == 'available'
    assert result.readme.text == '# Лабораторная\nРеализуйте сортировку.'
    assert result.readme.source_url == REPOSITORY + '#readme'
    assert [item.number for item in result.issues.items] == [1, 3]
    assert result.issues.items[1].url == REPOSITORY + '/issues/3'
    assert 'private' not in result.model_dump_json() and 'not-needed' not in result.model_dump_json()
    assert 'evil.example' not in result.model_dump_json()


@pytest.mark.parametrize('value', [
    'http://github.com/student/lab-work', 'https://github.com.evil.example/student/lab-work',
    'https://api.github.com/student/lab-work', 'https://localhost/student/lab-work',
    'https://user:secret@github.com/student/lab-work', 'https://github.com:443/student/lab-work',
    'https://github.com/student/lab-work/issues', 'https://github.com/student/lab-work?token=secret',
    'https://github.com/student/lab-work#readme', 'https://github.com/student/lab-work//',
    'https://github.com/student/%2e%2e', 'https://github.com/student/..',
    'https://git\nhub.com/student/lab-work', 'https://github.com/student/lab work',
    'https://github.com/student/lab-work?', 'https://github.com/student/lab-work#',
])
def test_only_repository_roots_are_accepted_before_any_network_request(value):
    def provider(request):
        pytest.fail('invalid repository must not reach the network')
    with pytest.raises(ValueError):
        read(provider, value)


def test_repository_extraction_handles_chat_links_and_rejects_ambiguous_repositories():
    assert normalize_github_repository('student/lab-work.git/') == REPOSITORY
    assert normalize_github_repository('https://GitHub.com/Student/Lab-Work.GIT/') == REPOSITORY
    assert extract_github_repository(f'{REPOSITORY} https://github.com/Student/LAB-Work') == REPOSITORY
    assert extract_github_repository(f'Разбери [{REPOSITORY}]({REPOSITORY}.git).') == REPOSITORY
    assert extract_github_repository(f'Посмотри {REPOSITORY}, пожалуйста.') == REPOSITORY
    assert extract_github_repository(f'{REPOSITORY} https://github.com/other/lab') is None
    assert extract_github_repository(REPOSITORY + '?token=secret') is None
    assert extract_github_repository(REPOSITORY + '/issues') is None
    assert extract_github_repository('https://user@github.com/student/lab-work') is None
    assert extract_github_repository('Без ссылки на репозиторий') is None


def test_missing_readme_and_real_empty_issues_is_successful_not_synthetic_data():
    result = read(lambda request: httpx.Response(404) if request.url.path.endswith('/readme')
        else httpx.Response(200, json=[]))
    assert result.status == 'available'
    assert result.readme.status == 'missing' and result.readme.text == ''
    assert result.issues.status == 'available' and result.issues.items == []


@pytest.mark.parametrize('status,headers,expected', [
    (403, {}, 'forbidden'), (403, {'x-ratelimit-remaining': '0'}, 'rate_limited'),
    (429, {}, 'rate_limited'), (403, {'retry-after': '30'}, 'rate_limited'),
    (410, {}, 'unavailable'), (503, {}, 'unavailable'), (404, {}, 'unavailable'),
])
def test_partial_provider_failures_preserve_real_readme_without_claiming_empty_issues(status, headers, expected):
    result = read(lambda request: httpx.Response(200, text='Actual requirements')
        if request.url.path.endswith('/readme') else
        httpx.Response(status, headers=headers, text='secret-provider-diagnostics'))
    assert result.status == 'partial' and result.readme.status == 'available'
    assert result.issues.status == expected and result.issues.items == []
    assert 'secret-provider' not in result.model_dump_json()


def test_both_missing_and_unavailable_sources_do_not_claim_repository_available():
    result = read(lambda request: httpx.Response(404))
    assert result.status == 'unavailable'
    assert result.readme.status == 'missing' and result.issues.status == 'unavailable'


def test_redirects_do_not_follow_new_hosts_or_forward_source_content():
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(302, headers={'Location': 'https://evil.example/private'})
    result = read(provider)
    assert len(calls) == 2 and {request.url.host for request in calls} == {'api.github.com'}
    assert result.status == 'unavailable'


def test_timeouts_are_typed_and_do_not_leak_transport_diagnostics():
    def provider(request):
        if request.url.path.endswith('/readme'):
            raise httpx.ReadTimeout('private details', request=request)
        return httpx.Response(200, json=[issue()])
    result = read(provider)
    assert result.status == 'partial' and result.readme.status == 'unavailable'
    assert len(result.issues.items) == 1 and 'private details' not in result.model_dump_json()


def test_readme_and_issue_caps_preserve_usable_context_and_report_truncation():
    result = read(lambda request: httpx.Response(200, content=b'a' * (MAX_RESPONSE_BYTES + 100))
        if request.url.path.endswith('/readme') else
        httpx.Response(200, json=[issue(number, body='b' * 1001) for number in range(1, 22)]))
    assert result.status == 'available'
    assert len(result.readme.text) == 12000 and result.readme.truncated
    assert len(result.issues.items) == 20 and result.issues.truncated
    assert len(result.issues.items[0].body) == 1000


def test_next_page_and_oversize_json_are_reported_truthfully():
    result = read(lambda request: httpx.Response(200, text='Requirements')
        if request.url.path.endswith('/readme') else
        httpx.Response(200, json=[issue()], headers={'Link': '<https://api.github.com/next>; rel="next"'}))
    assert result.issues.truncated and len(result.issues.items) == 1
    result = read(lambda request: httpx.Response(200, text='Requirements')
        if request.url.path.endswith('/readme') else
        httpx.Response(200, content=json.dumps([issue(body='x' * MAX_RESPONSE_BYTES)]).encode()))
    assert result.status == 'partial' and result.issues.status == 'unavailable'
    assert result.issues.truncated and not result.issues.items


@pytest.mark.parametrize('payload', [{}, ['invalid'], [issue(-1)], [issue(True)], [issue(body=32)]])
def test_malformed_issues_are_unavailable_instead_of_fabricated_empty_success(payload):
    result = read(lambda request: httpx.Response(200, text='Requirements')
        if request.url.path.endswith('/readme') else httpx.Response(200, json=payload))
    assert result.status == 'partial' and result.issues.status == 'unavailable'


def test_snapshot_rejects_cross_repository_source_links_and_false_availability():
    result = read(lambda request: httpx.Response(200, text='Requirements')
        if request.url.path.endswith('/readme') else httpx.Response(200, json=[issue()]))
    for field in ('readme', 'issue', 'availability'):
        value = result.model_dump(mode='json')
        if field == 'readme':
            value['readme']['source_url'] = 'https://evil.example/instructions'
        elif field == 'issue':
            value['issues']['items'][0]['url'] = 'https://github.com/other/lab/issues/1'
        else:
            value['status'] = 'unavailable'
        with pytest.raises(ValueError):
            GitHubRepositorySnapshot.model_validate(value)

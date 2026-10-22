"""Bounded, anonymous, read-only GitHub repository context for the study agent.

README and issue text are untrusted source material, never agent instructions.
The adapter only contacts api.github.com and does not follow source links.
"""
import asyncio
import json
import re
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

API_ORIGIN = 'https://api.github.com'
MAX_RESPONSE_BYTES = 65_536
MAX_README_CHARS = 12_000
MAX_ISSUES = 20
MAX_ISSUE_BODY_CHARS = 1_000
Status = Literal['available', 'missing', 'forbidden', 'rate_limited', 'unavailable']


def normalize_github_repository(value: str) -> str:
    """Accept a repository root (or owner/repository), never arbitrary URLs."""
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError('invalid_github_repository')
    value = value.strip()
    if re.search(r'\s', value):
        raise ValueError('invalid_github_repository')
    if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?', value):
        value = 'https://github.com/' + value
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.netloc.lower() != 'github.com' or
            '?' in value or '#' in value or parsed.path.endswith('//')):
        raise ValueError('invalid_github_repository')
    parts = parsed.path.rstrip('/').split('/')
    if len(parts) != 3 or parts[0]:
        raise ValueError('invalid_github_repository')
    owner, repository = parts[1:]
    if repository.lower().endswith('.git'):
        repository = repository[:-4]
    if (not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', owner) or
            not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', repository) or
            repository in ('.', '..')):
        raise ValueError('invalid_github_repository')
    return f'https://github.com/{owner.lower()}/{repository.lower()}'


def extract_github_repository(message: str) -> str | None:
    """Find the first explicit, valid repository root in a chat message."""
    if not isinstance(message, str):
        return None
    found = {}
    for candidate in re.findall(r'https://[^\s<>\[\]"\']+', message, flags=re.IGNORECASE):
        try:
            repository = normalize_github_repository(candidate.rstrip(').,;!'))
            found[repository.lower()] = repository
        except ValueError:
            continue
    return next(iter(found.values())) if len(found) == 1 else None


class _Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)


class GitHubReadme(_Contract):
    status: Status
    text: str = Field(default='', max_length=MAX_README_CHARS)
    source_url: str = Field(max_length=400)
    truncated: bool = False


class GitHubIssue(_Contract):
    number: StrictInt = Field(gt=0, le=2_147_483_647)
    title: str = Field(max_length=240)
    body: str = Field(max_length=MAX_ISSUE_BODY_CHARS)
    url: str = Field(max_length=400)


class GitHubIssues(_Contract):
    status: Literal['available', 'forbidden', 'rate_limited', 'unavailable']
    items: list[GitHubIssue] = Field(default_factory=list, max_length=MAX_ISSUES)
    truncated: bool = False


class GitHubRepositorySnapshot(_Contract):
    repository_url: str = Field(max_length=300)
    owner: str = Field(max_length=39)
    repository: str = Field(max_length=100)
    status: Literal['available', 'partial', 'unavailable']
    readme: GitHubReadme
    issues: GitHubIssues

    @model_validator(mode='after')
    def valid_sources(self):
        canonical = normalize_github_repository(self.repository_url)
        if canonical != self.repository_url or canonical != f'https://github.com/{self.owner}/{self.repository}':
            raise ValueError('invalid_github_source')
        if self.readme.source_url != canonical + '#readme':
            raise ValueError('invalid_github_readme_source')
        if any(issue.url != canonical + '/issues/' + str(issue.number) for issue in self.issues.items):
            raise ValueError('invalid_github_issue_source')
        if self.readme.status != 'available' and self.readme.text:
            raise ValueError('unavailable_github_readme_has_text')
        if self.issues.status != 'available' and self.issues.items:
            raise ValueError('unavailable_github_issues_have_items')
        expected = _overall_status(self.readme.status, self.issues.status)
        if self.status != expected:
            raise ValueError('invalid_github_snapshot_status')
        return self


def _overall_status(readme_status: str, issues_status: str) -> str:
    if issues_status == 'available' and readme_status in ('available', 'missing'):
        return 'available'
    if readme_status == 'available' or issues_status == 'available':
        return 'partial'
    return 'unavailable'


def _clean_text(value: str, maximum: int) -> str:
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)[:maximum]


class _ResponseFailure(Exception):
    def __init__(self, status='unavailable', *, truncated=False):
        self.status, self.truncated = status, truncated


async def _read(client: httpx.AsyncClient, path: str, *, readme=False):
    headers = {'Accept': 'application/vnd.github.raw+json' if readme else 'application/vnd.github+json'}
    async with client.stream('GET', API_ORIGIN + path, headers=headers) as response:
        if response.status_code == 404 and readme:
            raise _ResponseFailure('missing')
        if response.status_code == 429 or (response.status_code == 403 and
                (response.headers.get('x-ratelimit-remaining') == '0' or 'retry-after' in response.headers)):
            raise _ResponseFailure('rate_limited')
        if response.status_code in (401, 403):
            raise _ResponseFailure('forbidden')
        if response.status_code != 200:
            raise _ResponseFailure()
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                if readme:
                    raw.extend(chunk[:MAX_RESPONSE_BYTES - len(raw)])
                    return bytes(raw), True
                raise _ResponseFailure(truncated=True)
            raw.extend(chunk)
        return bytes(raw), 'rel="next"' in response.headers.get('link', '')


async def _readme(client: httpx.AsyncClient, path: str, repository_url: str) -> GitHubReadme:
    try:
        raw, truncated = await _read(client, path + '/readme', readme=True)
        text = raw.decode('utf-8', errors='replace')
        return GitHubReadme(status='available', text=_clean_text(text, MAX_README_CHARS),
            source_url=repository_url + '#readme', truncated=truncated or len(text) > MAX_README_CHARS)
    except (_ResponseFailure, httpx.HTTPError, TimeoutError) as error:
        return GitHubReadme(status=error.status if isinstance(error, _ResponseFailure) else 'unavailable',
            source_url=repository_url + '#readme')


async def _issues(client: httpx.AsyncClient, path: str, repository_url: str) -> GitHubIssues:
    try:
        raw, truncated = await _read(client, path + '/issues?state=open&per_page=30')
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise _ResponseFailure()
        items = []
        for issue in payload:
            if not isinstance(issue, dict):
                raise _ResponseFailure()
            if 'pull_request' in issue:
                continue
            if (type(issue.get('number')) is not int or not 0 < issue['number'] <= 2_147_483_647 or
                    not isinstance(issue.get('title'), str) or
                    (issue.get('body') is not None and not isinstance(issue['body'], str))):
                raise _ResponseFailure()
            if len(items) == MAX_ISSUES:
                truncated = True
                break
            title, body = issue['title'], issue.get('body') or ''
            truncated = truncated or len(title) > 240 or len(body) > MAX_ISSUE_BODY_CHARS
            items.append(GitHubIssue(number=issue['number'], title=_clean_text(title, 240),
                body=_clean_text(body, MAX_ISSUE_BODY_CHARS),
                url=repository_url + '/issues/' + str(issue['number'])))
        return GitHubIssues(status='available', items=items, truncated=truncated)
    except (_ResponseFailure, httpx.HTTPError, TimeoutError, ValueError, UnicodeError) as error:
        return GitHubIssues(status=error.status if isinstance(error, _ResponseFailure) else 'unavailable',
            truncated=error.truncated if isinstance(error, _ResponseFailure) else False)


async def read_github_repository(repository_url: str, *,
        transport: httpx.AsyncBaseTransport | None = None) -> GitHubRepositorySnapshot:
    """Read one public repository without GitHub credentials or write access."""
    repository_url = normalize_github_repository(repository_url)
    owner, repository = repository_url.removeprefix('https://github.com/').split('/')
    path = f'/repos/{owner}/{repository}'
    async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(8, connect=4),
            follow_redirects=False, trust_env=False, headers={
                'User-Agent': 'Google-AI-Student-Assistant', 'X-GitHub-Api-Version': '2022-11-28',
                'Accept-Encoding': 'identity'},
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2)) as client:
        try:
            async with asyncio.timeout(12):
                readme, issues = await asyncio.gather(
                    _readme(client, path, repository_url), _issues(client, path, repository_url))
        except TimeoutError:
            readme = GitHubReadme(status='unavailable', source_url=repository_url + '#readme')
            issues = GitHubIssues(status='unavailable')
    return GitHubRepositorySnapshot(repository_url=repository_url, owner=owner,
        repository=repository, status=_overall_status(readme.status, issues.status), readme=readme, issues=issues)

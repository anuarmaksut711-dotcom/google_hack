"""A timetable supplied in chat is a reviewable draft, never saved consent."""
import json

import pytest

from campuspulse_recovery.domain.chat_plan import render_advice
from campuspulse_recovery.domain.time_rules import instant


DRAFT = {'classes': [{'subject': 'Алгоритмы', 'weekday': 1, 'start': '12:00', 'end': '14:00'}],
         'valid_from': None, 'valid_until': None}


def render(draft, **changes):
    drafts, blocks = [], []
    text = render_advice(json.dumps({'kind': 'classes', 'message': 'Already saved!', 'blocks': [],
        'timetable': draft, **changes}), schedule=None, now=instant('2026-10-22T00:00:00Z'),
        timezone='Asia/Almaty', validated_blocks=blocks, validated_timetables=drafts)
    return text, drafts, blocks


def test_missing_dates_remain_empty_and_draft_never_claims_publication():
    text, drafts, blocks = render(DRAFT)
    assert drafts == [DRAFT] and blocks == []
    assert 'вторник' in text and '12:00' in text and 'Алгоритмы' in text
    assert 'Укажите даты' in text and 'Черновик' in text
    assert 'Already saved!' not in text


def test_explicit_period_is_shown_before_review():
    text, drafts, _ = render({**DRAFT, 'valid_from': '2026-10-22', 'valid_until': '2026-11-05'})
    assert '22.10.2026' in text and '05.11.2026' in text
    assert drafts[0]['valid_from'] == '2026-10-22'


@pytest.mark.parametrize('draft', [
    {**DRAFT, 'classes': DRAFT['classes']*2},
    {**DRAFT, 'classes': [{**DRAFT['classes'][0], 'weekday': True}]},
    {**DRAFT, 'classes': [{**DRAFT['classes'][0], 'end': '11:00'}]},
    {**DRAFT, 'valid_from': '2026-10-22', 'valid_until': '2027-10-22'},
    {**DRAFT, 'valid_from': 'tomorrow'},
    {**DRAFT, 'owner_id': 'other'},
])
def test_invalid_drafts_fail_without_publishing_model_text(draft):
    text, drafts, blocks = render(draft)
    assert 'Не удалось проверить формат' in text
    assert drafts == blocks == [] and 'Already saved!' not in text


def test_mixed_classes_and_study_blocks_are_rejected():
    text, drafts, blocks = render(DRAFT, blocks=[{'title': 'Study', 'start': '2026-10-23T12:00:00Z',
                                               'end': '2026-10-23T13:00:00Z'}])
    assert 'Не удалось проверить формат' in text and drafts == blocks == []

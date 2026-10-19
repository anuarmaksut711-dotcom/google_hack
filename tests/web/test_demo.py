"""Local demo: original production services, actual MCP protocol, fake IO only."""
import asyncio
from datetime import timedelta

import httpx
import pytest
from starlette.requests import Request

from campuspulse_recovery.demo.app import create_demo_app
from campuspulse_recovery.config import CLOUD_MARKERS
from campuspulse_recovery.domain.models import VerificationResult

ORIGIN='https://127.0.0.1:8947'


def run(coro):
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coro)


async def login(client):
    challenge=(await client.get('/auth/challenge')).json()
    response=await client.post('/auth/signin',headers={'Origin':ORIGIN,'X-CSRF-Token':challenge['csrf_token']},json={'credential':'local-demo-signin'})
    assert response.status_code==200,response.text
    return {'Origin':ORIGIN,'X-CSRF-Token':response.json()['csrf_token']}


def test_demo_requires_explicit_mode_and_literal_https_loopback():
    with pytest.raises(ValueError,match='explicit_local_demo'):
        create_demo_app()
    for origin in ('http://127.0.0.1:8947','https://example.com','https://localhost:8947'):
        with pytest.raises(ValueError,match='literal_loopback'):
            create_demo_app(demo=True,origin=origin)


@pytest.mark.parametrize('marker',sorted(CLOUD_MARKERS))
def test_demo_refuses_cloud_runtime(monkeypatch,marker):
    monkeypatch.setenv(marker,'synthetic-cloud-marker')
    with pytest.raises(ValueError,match='no_cloud_runtime'):
        create_demo_app(demo=True)


def test_real_services_mcp_approval_apply_fresh_verification_and_reload():
    async def scenario():
        app,system=create_demo_app(demo=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
                headers=await login(client)
                initial=await client.get('/ui/state')
                assert initial.status_code==200,initial.text
                assert 'Лабораторная работа' in initial.text and 'Занятие' in initial.text
                assert not system.workspace.writes
                proposal=await client.post('/api/runs',headers=headers,json={'request_key':'demo-propose'})
                assert proposal.status_code==200,proposal.text
                body=proposal.json()
                assert body['status']=='proposed',body
                ref=body['plan']
                payload={'plan':ref,'request_key':'demo-apply'}
                denied=await client.post('/api/runs/continue',headers=headers,json=payload)
                assert denied.status_code==403 and not system.workspace.writes
                mismatch=await client.post('/api/approval',headers=headers,json={**ref,'payload_hash':'0'*64,'request_key':'wrong-hash'})
                assert mismatch.status_code==409 and not system.workspace.writes
                approved=await client.post('/api/approval',headers=headers,json={**ref,'request_key':'demo-consent'})
                assert approved.status_code==200,approved.text
                assert not system.workspace.writes
                params={'plan_id':ref['plan_id'],'version':ref['version']}
                saved=(await client.get('/api/plan',params=params)).json()
                expected_ids=[op['operation_id'] for op in saved['plan']['payload']['operations']]
                assert saved['approval']['plan']==ref
                reads_before=len(system.workspace.reads)
                outcome=await client.post('/api/runs/continue',headers=headers,json=payload)
                assert outcome.status_code==200,outcome.text
                result=outcome.json()
                assert result['status']=='verified' and result['verified_success'],result
                assert result['verification_at'] and result['verification_run_id']==result['run_id']
                assert len(system.workspace.writes)==len(expected_ids)>0
                assert len(system.workspace.reads)>reads_before
                assert any(kind=='calendar' and run_id==result['run_id'] for kind,run_id in system.workspace.reads)
                final=await client.get('/ui/state',params={'run_id':result['run_id']})
                assert final.status_code==200,final.text
                assert 'Расписание проверено' in final.text and 'Проверено:' in final.text
                assert 'ДЕМО' in final.text and all(identity in final.text for identity in expected_ids)
                assert 'data-action="apply"' not in final.text
                count=len(system.workspace.writes)
                assert (await client.post('/api/runs/continue',headers=headers,json=payload)).json()==result
                assert len(system.workspace.writes)==count
                assert (await client.get('/api/runs/'+result['run_id'])).json()==result
                records=[]; cursor=0
                while True:
                    page=(await client.get('/api/runs/'+result['run_id']+'/trace',params={'after':cursor})).json()
                    records.extend(page['records'])
                    if page['next']==cursor:break
                    cursor=page['next']
                assert {r['observation']['event']['tool'] for r in records}>={'apply_plan','verify_plan','read_calendar'}
                assert set(expected_ids)<={r['observation']['event']['operation_id'] for r in records}
                assert any(r['observation']['attempt_id'] for r in records)
                again=(await client.post('/api/runs',headers=headers,json={'request_key':'demo-no-change'})).json()
                assert again['status']=='no_change',again
                assert len(system.workspace.writes)==count

                event_id=system.workspace.writes[0]['event_id']
                from datetime import datetime
                raw=system.workspace.events[event_id]
                raw['end']['dateTime']=(datetime.fromisoformat(raw['end']['dateTime'])+timedelta(minutes=15)).isoformat()
                cookie='; '.join(k+'='+v for k,v in client.cookies.items())
                request=Request({'type':'http','method':'GET','path':'/','scheme':'https',
                    'server':('127.0.0.1',8947),'headers':[(b'host',b'127.0.0.1:8947'),(b'cookie',cookie.encode())]})
                credential=await system.runtime.auth.load(request)
                with system.runtime.auth.request_scope(credential) as context:
                    failed=await system.call(context,'verify_plan',{'plan_id':ref['plan_id'],'version':ref['version']},VerificationResult)
                assert failed.status=='verification_failed',failed
                assert any(f.code=='verify_event_target_mismatch' for f in failed.findings)
                assert len(system.workspace.writes)==count
                historical=await client.get('/ui/state',params={'run_id':result['run_id']})
                assert historical.status_code==200
                assert 'Расписание проверено' not in historical.text
    run(scenario())

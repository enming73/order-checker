import json
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from app.config import ROOT
from app.service import Conflict
from test_business import run_case


def wire_erp(application, client, monkeypatch):
    """HTTP adapter to the actual mock ERP ASGI handlers; no socket required."""
    def get(url, **kwargs):
        return client.get('/mock-erp/' + url.split('/mock-erp/')[1], headers=kwargs.get('headers', {}))
    def post(url, **kwargs):
        return client.post('/mock-erp/' + url.split('/mock-erp/')[1], headers=kwargs.get('headers', {}), json=kwargs['json'])
    monkeypatch.setattr('app.service.httpx.get', get)
    monkeypatch.setattr('app.service.httpx.post', post)
    return post


def test_full_web_journey_and_ambiguous_erp_commit(application, client, monkeypatch):
    service=application.state.service
    post = wire_erp(application,client,monkeypatch)
    def trigger(case_id):
        run=service.new_run(case_id)
        for step in ('parse','extract','validate'):
            service.step(case_id,run,step)
        return run
    monkeypatch.setattr(service,'trigger',trigger)
    raw=(ROOT/'samples/Auftrag-C.eml').read_bytes()
    response=client.post('/upload',files={'file':('Auftrag-C.eml',raw,'message/rfc822')},follow_redirects=False)
    assert response.status_code==303
    location=response.headers['location'];case_id=location.rsplit('/',1)[1]
    page=client.get(location)
    assert page.status_code==200
    assert 'E-Mail: <strong>Rev. C' in page.text
    assert 'PDF, Seite 1: <strong>Rev. B' in page.text
    assert 'Welcher Zeichnungsstand' in page.text
    assert 'Demo-Modus · vorbereitete KI-Antworten' in page.text
    assert 'Prüfung abschließen' not in page.text
    duplicate=client.post('/upload',files={'file':('copy.eml',raw)},follow_redirects=False)
    assert duplicate.headers['location']==location
    assert client.get(location+'/original').content==raw
    assert client.post(location+'/review',data={'decision':'complete'}).status_code==409
    # ERP commits successfully, then the caller loses the response.
    def committed_timeout(url,**kwargs):
        response=post(url,**kwargs)
        assert response.status_code==200
        raise httpx.ReadTimeout('response lost')
    monkeypatch.setattr('app.service.httpx.post',committed_timeout)
    custom='Guten Tag, bitte bestätigen Sie den verbindlichen Zeichnungsstand. Vielen Dank.'
    response=client.post(location+'/review',data={'decision':'clarify','draft':custom})
    assert response.status_code==200
    assert 'ERP-Synchronisierung ausstehend' in response.text
    assert custom in response.text
    event=client.get('/api/cases/'+case_id).json()['review']['id']
    with service.db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM reviews').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM erp_results').fetchone()[0]==1
    monkeypatch.setattr('app.service.httpx.post',post)
    assert 'ERP synchronisiert' in client.post(location+'/sync').text
    client.post(location+'/sync')
    client.post(location+'/review',data={'decision':'clarify','draft':'Doppelter Klick'})
    with service.db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM reviews').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM erp_results').fetchone()[0]==1
        review=conn.execute('SELECT * FROM reviews').fetchone()
        assert review['draft']==custom and review['id']==event and review['synced']==1
    assert 'Vorprüfung erneut auslösen' not in client.get(location).text


def test_completion_writes_expected_erp_status(application, client, monkeypatch):
    wire_erp(application,client,monkeypatch)
    service=application.state.service
    case_id,_=run_case(service,'A')
    result=client.post(f'/cases/{case_id}/review', data={'decision':'complete'})
    assert 'Vorprüfung abgeschlossen' in result.text
    payload=client.get('/mock-erp/results',headers={'X-Internal-Token':application.state.settings.internal_token}).json()
    assert len(payload)==1 and payload[0]['status']=='Vorprüfung abgeschlossen'


def test_write_failure_keeps_review_and_retry(application,client,monkeypatch):
    post=wire_erp(application,client,monkeypatch)
    service=application.state.service
    case_id,_=run_case(service,'B')
    monkeypatch.setattr('app.service.httpx.post',lambda *a,**kw: (_ for _ in ()).throw(httpx.ReadTimeout('timeout')))
    event=service.review(case_id,'clarify',service.db.case(case_id)['draft'])
    assert not service.sync(case_id)
    with service.db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM erp_results').fetchone()[0]==0
        assert conn.execute('SELECT id FROM reviews').fetchone()[0]==event
    monkeypatch.setattr('app.service.httpx.post',post)
    assert service.sync(case_id)
    assert service.sync(case_id)
    with service.db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM erp_results').fetchone()[0]==1


def test_erp_rejects_changed_idempotency_payload(client,application):
    headers={'X-Internal-Token':application.state.settings.internal_token}
    payload={'event_id':'event-1','case_id':'case-1','status':'Vorprüfung abgeschlossen',
             'reviewed_at':'2026-11-12','reviewer':'Test','draft':'','result':{}}
    assert client.post('/mock-erp/results',headers=headers,json=payload).status_code==200
    assert client.post('/mock-erp/results',headers=headers,json=payload).status_code==200
    payload['draft']='changed'
    assert client.post('/mock-erp/results',headers=headers,json=payload).status_code==409


def test_security_and_invalid_uploads(client):
    assert client.post('/internal/steps/parse',json={'case_id':'x','run_id':'y'}).status_code==401
    assert client.get('/mock-erp/results').status_code==401
    assert client.post('/upload',headers={'Origin':'https://other.example'},files={'file':('x.eml',b'x')}).status_code==403
    assert client.post('/upload',files={'file':('x.txt',b'x')}).status_code==409
    assert client.post('/upload',files={'file':('x.eml',b'')}).status_code==409
    assert client.post('/upload',files={'file':('x.eml',b'x'*(12*1024*1024+1))}).status_code==409
    assert client.get('/cases/nonexistent').status_code==404
    assert client.get('/samples/C').content==(ROOT/'samples/Auftrag-C.eml').read_bytes()


def test_html_is_escaped_and_fault_controls_work(client,application):
    service=application.state.service
    case_id,_=service.upload(b'test','<script>alert(1)</script>.eml')
    assert '<script>alert(1)</script>' not in client.get('/cases/'+case_id).text
    assert client.post('/demo/faults',data={'name':'write','mode':'after_commit_timeout'}).status_code==200
    with service.db.connect() as conn:
        assert conn.execute("SELECT mode FROM faults WHERE name='write'").fetchone()[0]=='after_commit_timeout'


def test_workflow_artifact_is_metadata_only():
    workflow=json.loads((ROOT/'n8n/workflow.json').read_text())
    assert workflow['id']=='schaltschrankReview01'
    requests=[n for n in workflow['nodes'] if n['type']=='n8n-nodes-base.httpRequest']
    assert len(requests)==4
    for node in requests:
        body=node['parameters']['jsonBody']
        assert 'case_id' in body and 'run_id' in body
        for forbidden in ('attachments','model_response','quote','raw'):
            assert forbidden not in body
    assert len([n for n in workflow['nodes'] if n['type']=='n8n-nodes-base.if'])==3

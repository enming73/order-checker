import json
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from io import BytesIO
from threading import Event
import httpx
import pytest
from pypdf import PdfWriter
from app.config import ROOT, Settings
from app.db import Database
from app.domain import Extraction, validate_evidence, normalize, check_rules
from app.parsing import parse_email
from app.service import Service, Conflict


def run_case(service, letter):
    case_id, _ = service.upload((ROOT / f"samples/Auftrag-{letter}.eml").read_bytes(), f"{letter}.eml")
    run = service.new_run(case_id)
    for step in ('parse', 'extract', 'validate'):
        assert service.step(case_id, run, step)['ok']
    return case_id, run


@pytest.mark.parametrize('letter,expected', [('A', set()), ('B', {'missing'}), ('C', {'conflict', 'erp_conflict'})])
def test_abc(application, monkeypatch, letter, expected):
    service = application.state.service
    monkeypatch.setattr(service, 'lookup', lambda drawing: {'status':'ok', 'revision': 'A' if drawing == 'SZ-4101' else 'C'})
    case_id, run = run_case(service, letter)
    case = service.db.case(case_id)
    assert case['status'] == 'ready'
    assert {p['code'] for p in case['issues']} == expected
    assert all(c['verified'] for values in case['fields'].values() for c in values)
    if letter == 'B':
        assert 'IP-Schutzart' in case['draft']
        assert 'Zeichnungsstand' not in case['draft']
    if letter == 'C':
        assert [c['value'] for c in case['fields']['drawing_revision']] == ['Rev. C', 'Rev. B']
        assert 'Welcher Zeichnungsstand' in case['draft']
        assert case['draft'].count('Welcher Zeichnungsstand') == 1
    with service.db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM attachments WHERE case_id=?',(case_id,)).fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM steps WHERE case_id=?',(case_id,)).fetchone()[0] == 3


def test_evidence_rejects_fabrication_and_wrong_source():
    response = json.loads((ROOT/'samples/responses/C.json').read_text())
    sources = parse_email((ROOT/'samples/Auftrag-C.eml').read_bytes(), Settings())[1]
    response['ip_rating'][0]['value'] = 'IP65'
    response['quantity'][0]['value'] = '2026'  # A number elsewhere in the quote is not the quantity.
    response['quantity'][0]['evidence']['quote'] = 'Auftragsnummer: AB-2026-0143'
    response['drawing_revision'][1]['evidence']['page'] = 2
    fields = validate_evidence(Extraction.model_validate(response), sources)
    assert not fields['ip_rating'][0]['verified']
    assert not fields['quantity'][0]['verified']
    assert not fields['drawing_revision'][1]['verified']
    assert fields['drawing_revision'][0]['verified']


@pytest.mark.parametrize('value',['Rev. C','rev c','Revision C', 'C', ' Stand C '])
def test_revision_normalization(value):
    assert normalize('drawing_revision', value) == 'C'


def test_upload_and_review_concurrent_idempotency(application, monkeypatch):
    service = application.state.service
    raw = (ROOT/'samples/Auftrag-A.eml').read_bytes()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: service.upload(raw,'A.eml'), range(12)))
    assert len({case for case, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    monkeypatch.setattr(service, 'lookup', lambda _: {'status':'ok','revision':'A'})
    case_id, run = run_case(service, 'A')
    with ThreadPoolExecutor(max_workers=6) as pool:
        events = list(pool.map(lambda _: service.review(case_id,'complete',''), range(12)))
    assert len(set(events)) == 1
    assert service.step(case_id, run, 'parse')['result'] == 'ignored'
    service.fail(case_id, run, 'transport', 'old error')
    assert service.db.case(case_id)['status'] == 'reviewed'
    with pytest.raises(Conflict):
        service.new_run(case_id)


def test_old_inflight_callback_cannot_overwrite_review(application, monkeypatch):
    service = application.state.service
    case_id, _ = service.upload((ROOT/'samples/Auftrag-A.eml').read_bytes(),'A.eml')
    old = service.new_run(case_id)
    started, release = Event(), Event()
    from app import service as module
    original = module.parse_email
    def blocked(*args):
        started.set()
        release.wait(5)
        return original(*args)
    monkeypatch.setattr(module, 'parse_email', blocked)
    with ThreadPoolExecutor() as pool:
        task = pool.submit(service.step, case_id, old, 'parse')
        assert started.wait(2)
        assert service.step(case_id, old, 'parse')['result'] == 'busy_or_out_of_order'
        new = service.new_run(case_id)
        monkeypatch.setattr(module, 'parse_email', original)
        monkeypatch.setattr(service, 'lookup', lambda _: {'status':'ok','revision':'A'})
        for step in ('parse','extract','validate'):
            service.step(case_id,new,step)
        event = service.review(case_id,'complete','')
        release.set()
        assert task.result()['result'] == 'ignored'
    assert service.db.case(case_id)['status'] == 'reviewed'
    with service.db.connect() as conn:
        assert conn.execute('SELECT id FROM reviews').fetchone()[0] == event


def test_duplicate_stage_and_out_of_order(application):
    service = application.state.service
    case_id,_ = service.upload((ROOT/'samples/Auftrag-A.eml').read_bytes(),'A.eml')
    run=service.new_run(case_id)
    assert not service.step(case_id,run,'extract')['ok']
    assert service.step(case_id,run,'parse')['ok']
    assert service.step(case_id,run,'parse')['result']=='already_done'
    assert len(service.db.sources(case_id)) == 2


def test_unknown_input_and_invalid_model(application, monkeypatch):
    service=application.state.service
    raw=(ROOT/'samples/Auftrag-A.eml').read_bytes().replace(b'Lena Beispiel',b'Lisa Beispiel')
    case_id,_=service.upload(raw,'unknown.eml'); run=service.new_run(case_id)
    service.step(case_id,run,'parse'); result=service.step(case_id,run,'extract')
    assert result['result']=='manual'
    assert 'Keine vorbereitete' in service.db.case(case_id)['failure_reason']
    monkeypatch.setattr('app.service.extract', lambda *args: '{"secret": "not logged"}')
    run=service.new_run(case_id); service.step(case_id,run,'parse');service.step(case_id,run,'extract')
    case=service.db.case(case_id)
    assert 'Schema' in case['failure_reason'] and 'not logged' not in case['failure_reason']
    assert 'not logged' in case['model_response']
    with pytest.raises(Conflict):service.review(case_id,'complete','')


def test_erp_status_distinction(application, monkeypatch):
    service=application.state.service
    monkeypatch.setattr('app.service.httpx.get', lambda *a,**kw: (_ for _ in ()).throw(httpx.ReadTimeout('timeout')))
    assert service.lookup('x')['status']=='timeout'
    monkeypatch.setattr('app.service.httpx.get', lambda *a,**kw: httpx.Response(404))
    assert service.lookup('x')['status']=='not_found'
    assert check_rules({}, {'status':'timeout'})[-1]['code']=='erp_timeout'
    assert check_rules({}, {'status':'not_found'})[-1]['code']=='erp_not_found'


def test_html_and_damaged_scanned_large_pdf():
    msg=EmailMessage();msg['From']='example@example.test';msg['Subject']='HTML'
    msg.set_content('<html><style>bad</style><p>Auftragsnummer: AB-42</p><script>bad</script></html>',subtype='html')
    writer=PdfWriter();writer.add_blank_page(100,100);out=BytesIO();writer.write(out)
    msg.add_attachment(out.getvalue(),maintype='application',subtype='pdf',filename='scan.pdf')
    msg.add_attachment(b'broken',maintype='application',subtype='pdf',filename='broken.pdf')
    subject,sources,attachments,warnings=parse_email(msg.as_bytes(),Settings())
    assert 'AB-42' in sources[0]['text'] and 'bad' not in sources[0]['text']
    assert any('Scan' in w for w in warnings) and any('beschädigt' in w for w in warnings)
    assert len(attachments)==2
    assert 'überschreitet' in parse_email(msg.as_bytes(),Settings(max_attachment=2))[3][0]


def test_failed_trigger_preserves_original(application, monkeypatch):
    service=application.state.service
    case_id,_=service.upload(b'test','test.eml')
    monkeypatch.setattr('app.service.httpx.post',lambda *a,**kw: (_ for _ in ()).throw(httpx.ConnectError('no network')))
    service.trigger(case_id)
    case=service.db.case(case_id)
    assert case['raw']==b'test' and case['failure_step']=='trigger'
    assert case['status']=='manual'


def test_persistence_across_application_restart(application, monkeypatch):
    service=application.state.service
    monkeypatch.setattr(service,'lookup',lambda _: {'status':'ok','revision':'A'})
    case_id,_=run_case(service,'A')
    event=service.review(case_id,'complete','')
    reopened=Database(application.state.settings.data_dir)
    assert reopened.case(case_id)['status']=='reviewed'
    assert len(reopened.sources(case_id))==2
    with reopened.connect() as conn:
        assert conn.execute('SELECT id FROM reviews WHERE case_id=?',(case_id,)).fetchone()[0]==event


def test_live_configuration_and_structured_response(monkeypatch):
    from app.model import extract
    from app.parsing import ManualCheck
    with pytest.raises(ManualCheck,match='API-Key'):
        extract(Settings(llm_mode='live',llm_key='',llm_model=''), 'unused', [])
    expected=(ROOT/'samples/responses/A.json').read_text()
    def provider(url,**kwargs):
        assert url=='https://model.example/v1/chat/completions'
        assert kwargs['json']['response_format']['json_schema']['strict']
        assert kwargs['headers']['Authorization']=='Bearer test-key'
        return httpx.Response(200,json={'choices':[{'message':{'content':expected}}]},request=httpx.Request('POST',url))
    monkeypatch.setattr('app.model.httpx.post',provider)
    response=extract(Settings(llm_mode='live',llm_url='https://model.example/v1',llm_key='test-key',llm_model='test-model'),'unused',[])
    assert Extraction.model_validate_json(response).quantity[0].value=='2'


def test_late_sync_failure_does_not_revoke_success(application,monkeypatch):
    service=application.state.service
    monkeypatch.setattr(service,'lookup',lambda _: {'status':'ok','revision':'A'})
    case_id,_=run_case(service,'A');service.review(case_id,'complete','')
    started,release=Event(),Event()
    def slow_fail(*args,**kwargs):
        started.set();release.wait(3)
        raise httpx.ReadTimeout('late failure')
    monkeypatch.setattr('app.service.httpx.post',slow_fail)
    with ThreadPoolExecutor() as pool:
        slow=pool.submit(service.sync,case_id)
        assert started.wait(2)
        monkeypatch.setattr('app.service.httpx.post',lambda *a,**kw: httpx.Response(200,request=httpx.Request('POST','http://erp/results')))
        assert service.sync(case_id)
        release.set();assert slow.result() is False
    with service.db.connect() as conn:
        row=conn.execute('SELECT synced,sync_error FROM reviews').fetchone()
        assert row['synced']==1 and row['sync_error'] is None

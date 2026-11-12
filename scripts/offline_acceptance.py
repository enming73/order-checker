"""Deterministic acceptance without network/n8n; exports inspectable HTML snapshots.

Uses real FastAPI routes, multipart uploads, parsers and SQLite. Only the n8n
transport and ERP HTTP socket are replaced with in-process adapters.
"""
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from asgi_client import ASGIClient
from app.config import Settings
from app.main import create_app


def main():
    output=ROOT/'output/offline'
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        app=create_app(Settings(data_dir=Path(temp)))
        client=ASGIClient(app);service=app.state.service
        def erp_get(url,**kw):
            return client.get('/mock-erp/'+url.split('/mock-erp/')[1],headers=kw['headers'])
        def erp_post(url,**kw):
            return client.post('/mock-erp/'+url.split('/mock-erp/')[1],headers=kw['headers'],json=kw['json'])
        def trigger(case_id):
            run=service.new_run(case_id)
            for step in ('parse','extract','validate'):
                assert service.step(case_id,run,step)['ok']
        def snapshot(name,response):
            assert response.status_code==200
            css=(ROOT/'app/static/style.css').read_text()
            html=response.text.replace('<link rel="stylesheet" href="/static/style.css">','<style>'+css+'</style>').replace('<script src="/static/app.js" defer></script>','')
            (output/name).write_text(html)
        with patch('app.service.httpx.get',erp_get),patch('app.service.httpx.post',erp_post),patch.object(service,'trigger',trigger):
            for letter in 'ABC':
                raw=(ROOT/f'samples/Auftrag-{letter}.eml').read_bytes()
                response=client.post('/upload',files={'file':(f'{letter}.eml',raw)},follow_redirects=False)
                path=response.headers['location'];case_id=path.rsplit('/',1)[1]
                case=client.get('/api/cases/'+case_id).json()
                assert case['status']=='ready'
                snapshot(f'{letter}-before.html',client.get(path))
                reviewed=client.post(path+'/review',data={'decision':'complete' if letter=='A' else 'clarify','draft':case['draft']})
                snapshot(f'{letter}-reviewed.html',reviewed)
                assert client.get('/api/cases/'+case_id).json()['review']['synced']==1
                print(f'{letter}: {len(case["issues"])} Prüfpunkte, menschliche Entscheidung und ERP gespeichert.')
            snapshot('index.html',client.get('/'))
            results=client.get('/mock-erp/results',headers={'X-Internal-Token':app.state.settings.internal_token}).json()
            assert len(results)==3
            print('Offline-Abnahme bestanden. HTML: output/offline/ (keine Browser- oder n8n-Abnahme).')

if __name__=='__main__':main()

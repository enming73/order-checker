"""Real HTTP / n8n acceptance. Run against a fresh Compose project, never a unit-test stub."""
import argparse
import os
from pathlib import Path
import subprocess
import time
import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://localhost:8000')
    parser.add_argument('--n8n-url', default='http://localhost:5678')
    parser.add_argument('--restart-n8n', action='store_true')
    args = parser.parse_args()
    token = os.getenv('INTERNAL_TOKEN', 'local-demo-token-change-me')
    # Match a copied .env without executing shell contents or printing secrets.
    if 'INTERNAL_TOKEN' not in os.environ and (ROOT/'.env').exists():
        for line in (ROOT/'.env').read_text().splitlines():
            if line.startswith('INTERNAL_TOKEN='):
                token = line.split('=',1)[1].strip().strip('"\'')
    with httpx.Client(base_url=args.base_url, timeout=15, follow_redirects=True) as client:
        def wait_case(case_id):
            for _ in range(90):
                response = client.get('/api/cases/'+case_id); response.raise_for_status()
                case=response.json()
                if case['status'] in ('ready','manual','reviewed'):
                    return case
                time.sleep(1)
            raise AssertionError('Workflow hat innerhalb von 90 Sekunden kein Ergebnis geliefert.')
        def fault(name,mode):
            client.post('/demo/faults',data={'name':name,'mode':mode}).raise_for_status()
        for name in ('read','write'):fault(name,'normal')
        for letter in 'ABC':
            if letter == 'C' and args.restart_n8n:
                subprocess.run(['docker','compose','restart','n8n'], cwd=ROOT, check=True)
                for _ in range(90):
                    try:
                        if httpx.get(args.n8n_url+'/healthz',timeout=2).status_code==200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(1)
                else:
                    raise AssertionError('n8n nach Neustart nicht erreichbar')
                # Health endpoint may precede webhook registration.
                time.sleep(3)
            raw=(ROOT/f'samples/Auftrag-{letter}.eml').read_bytes()
            response=client.post('/upload',files={'file':(f'Auftrag-{letter}.eml',raw,'message/rfc822')},follow_redirects=False)
            assert response.status_code==303, response.status_code
            path=response.headers['location'];case_id=path.rsplit('/',1)[1]
            duplicate=client.post('/upload',files={'file':('copy.eml',raw)},follow_redirects=False)
            assert duplicate.headers['location']==path
            case=wait_case(case_id)
            assert case['review'] is None, 'Für diesen Test einen frischen Compose-Projektnamen verwenden.'
            assert case['status']=='ready', (letter,case['failure_reason'])
            if letter=='A':
                assert not case['issues']
                # Exercise read timeout and explicit re-processing on the same case.
                fault('read','timeout');client.post(path+'/retry').raise_for_status()
                assert 'erp_timeout' in {i['code'] for i in wait_case(case_id)['issues']}
                fault('read','normal');client.post(path+'/retry').raise_for_status()
                case=wait_case(case_id);assert not case['issues']
            if letter=='B':
                assert 'IP-Schutzart' in case['draft']
            if letter=='C':
                page=client.get(path).text
                assert 'E-Mail: <strong>Rev. C' in page and 'PDF, Seite 1: <strong>Rev. B' in page
                assert 'Welcher Zeichnungsstand' in case['draft']
                fault('write','after_commit_timeout')
            data={'decision':'complete' if letter=='A' else 'clarify','draft':case['draft']}
            client.post(path+'/review',data=data).raise_for_status()
            reviewed=client.get('/api/cases/'+case_id).json()
            assert reviewed['review']
            if letter=='C':
                assert not reviewed['review']['synced']
                assert 'ERP-Synchronisierung ausstehend' in client.get(path).text
                fault('write','normal')
                client.post(path+'/sync').raise_for_status()
            client.post(path+'/review',data=data).raise_for_status()
            client.post(path+'/sync').raise_for_status()
            assert client.get('/api/cases/'+case_id).json()['review']['synced']
            results=client.get('/mock-erp/results',headers={'X-Internal-Token':token})
            results.raise_for_status()
            assert sum(r['case_id']==case_id for r in results.json())==1
            print(f'{letter}: Belege, Prüfung, Duplikate und ERP geprüft.')
        print('HTTP-/n8n-Abnahme bestanden.' + (' Neustart geprüft.' if args.restart_n8n else ' Neustart nicht geprüft.'))


if __name__=='__main__':
    main()

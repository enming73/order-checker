"""Build a minimal n8n workflow: only IDs and stage summaries cross the boundary."""
import json
from pathlib import Path

nodes = [{"parameters": {"httpMethod": "POST", "path": "auftragspruefung", "responseMode": "onReceived", "options": {}},
          "id": "ingress", "name": "Auftrag empfangen", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
          "position": [0, 0], "webhookId": "order-review-webhook"}]
connections = {}

def link(name, targets):
    connections[name] = {"main": [[{"node": target, "type": "main", "index": 0} for target in branch] for branch in targets]}

for index, (step, name) in enumerate([("parse", "E-Mail und PDF lesen"), ("extract", "Felder und Belege extrahieren"), ("validate", "Regeln und ERP abgleichen")]):
    nodes.append({"parameters": {"method": "POST", "url": f"http://app:8000/internal/steps/{step}",
        "sendHeaders": True, "headerParameters": {"parameters": [{"name": "X-Internal-Token", "value": "={{ $env.INTERNAL_TOKEN }}"}]},
        "sendBody": True, "specifyBody": "json", "jsonBody": "={{ { case_id: $('Auftrag empfangen').item.json.body.case_id, run_id: $('Auftrag empfangen').item.json.body.run_id } }}",
        "options": {"timeout": 90000}}, "id": step, "name": name, "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2, "position": [260 + index*520, 0], "onError": "continueErrorOutput"})
    condition = name + " – erfolgreich?"
    nodes.append({"parameters": {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "conditions": [{"id": "ok-"+step, "leftValue": "={{ $json.ok }}", "rightValue": True,
                        "operator": {"type": "boolean", "operation": "true", "singleValue": True}}], "combinator": "and"}, "options": {}},
        "id": "if-"+step, "name": condition, "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [500+index*520, 0]})
    link(name, [[condition], ["Verbindungsfehler speichern"]])
    next_name = ["Felder und Belege extrahieren", "Regeln und ERP abgleichen", "Bereit zur Prüfung"][index]
    link(condition, [[next_name], ["Manuelle Prüfung oder alter Lauf"]])
link("Auftrag empfangen", [["E-Mail und PDF lesen"]])
for name, identifier, pos in [("Bereit zur Prüfung", "done", [1820, 0]), ("Manuelle Prüfung oder alter Lauf", "manual", [1040, 300])]:
    nodes.append({"parameters": {}, "id": identifier, "name": name, "type": "n8n-nodes-base.noOp", "typeVersion": 1, "position": pos})
nodes.append({"parameters": {"method": "POST", "url": "http://app:8000/internal/failure",
    "sendHeaders": True, "headerParameters": {"parameters": [{"name": "X-Internal-Token", "value": "={{ $env.INTERNAL_TOKEN }}"}]},
    "sendBody": True, "specifyBody": "json", "jsonBody": "={{ { case_id: $('Auftrag empfangen').item.json.body.case_id, run_id: $('Auftrag empfangen').item.json.body.run_id, step: 'transport' } }}",
    "options": {"timeout": 10000}}, "id": "failure", "name": "Verbindungsfehler speichern", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
    "position": [1040, 500], "retryOnFail": True, "maxTries": 3, "waitBetweenTries": 1000})
workflow = {"id": "schaltschrankReview01", "name": "Schaltschrank · Technische Vorprüfung", "active": False,
            "nodes": nodes, "connections": connections, "settings": {"executionOrder": "v1", "saveDataSuccessExecution": "all", "saveDataErrorExecution": "all"},
            "versionId": "a09a10ce-d023-47bf-93c7-ae8c168eb0cd", "pinData": {}, "tags": []}
(Path(__file__).resolve().parents[1] / "n8n/workflow.json").write_text(json.dumps(workflow, ensure_ascii=False, indent=2))

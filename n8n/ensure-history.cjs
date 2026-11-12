// n8n 2.0.3 CLI imports do not create workflow_history, but publication requires it.
// Scoped compatibility repair, before n8n starts; touches only this demo workflow.
// Remove after upgrading to a version whose CLI import creates publishable history.
const fs = require('node:fs');
const crypto = require('node:crypto');
const sqlite = require(require.resolve('sqlite3', { paths: ['/usr/local/lib/node_modules/n8n'] }));
const source = fs.readFileSync('/config/workflow.json');
const workflow = JSON.parse(source);
if (workflow.id !== 'schaltschrankReview01') throw new Error('Unexpected workflow ID');
const hex = crypto.createHash('sha256').update(source).digest('hex').slice(0, 32);
const version = [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-');
const db = new sqlite.Database('/home/node/.n8n/database.sqlite');
const run = (sql, args = []) => new Promise((resolve, reject) => db.run(sql, args, function (error) { error ? reject(error) : resolve(this); }));
(async () => {
  try {
    await run('BEGIN IMMEDIATE');
    const updated = await run('UPDATE workflow_entity SET versionId=? WHERE id=?', [version, workflow.id]);
    if (updated.changes !== 1) throw new Error('Imported workflow missing');
    await run(`INSERT OR IGNORE INTO workflow_history (versionId,workflowId,authors,nodes,connections,name,autosaved)
      SELECT versionId,id,?,nodes,connections,name,0 FROM workflow_entity WHERE id=?`, ['Demo bootstrap', workflow.id]);
    await run('COMMIT');
    console.log('Demo workflow history verified.');
  } catch (error) {
    await run('ROLLBACK').catch(() => {});
    console.error('Workflow history initialization failed:', error.message);
    process.exitCode = 1;
  } finally {
    db.close();
  }
})();

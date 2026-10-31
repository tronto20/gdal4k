const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {DefaultArtifactClient} = require('@actions/artifact');
async function main() {
  const directory = path.resolve(process.env.INPUT_PATH || '');
  const prefix = process.env['INPUT_NAME-PREFIX'];
  const days = Number(process.env['INPUT_RETENTION-DAYS']);
  if (!/^[A-Za-z0-9][A-Za-z0-9-]{0,180}$/.test(prefix || '') || !Number.isSafeInteger(days) || days < 1 || days > 90) throw Error('proof upload inputs');
  if (fs.realpathSync(directory) !== directory || !fs.lstatSync(directory).isDirectory()) throw Error('canonical proof directory');
  const indexPath = path.join(directory, 'proof-index.json');
  if (!fs.existsSync(indexPath)) {
    const names = fs.readdirSync(directory).filter(n => n === 'failure.json' || /^part-[0-9]{4}\.bin$/.test(n)).sort();
    if (!names.includes('failure.json')) throw Error('missing complete index and bounded failure record');
    const client = new DefaultArtifactClient(), uploads = [];
    for (const name of names) {
      const filename = path.join(directory, name), s = fs.lstatSync(filename);
      const limit = name === 'failure.json' ? 65536 : 24*1048576-65536;
      if (!s.isFile() || s.isSymbolicLink() || s.nlink !== 1 || s.size > limit) throw Error('partial evidence bound');
      const sha256 = crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex');
      const result = await client.uploadArtifact(prefix+'-incomplete-'+name.replace(/\./g,'-'), [filename], directory, {compressionLevel:0,retentionDays:days});
      uploads.push({file:name,bytes:s.size,sha256,result:{id:result.id,size:result.size,digest:result.digest}});
    }
    const summary = path.join(directory, 'incomplete-uploads.json');
    fs.writeFileSync(summary, JSON.stringify({schemaVersion:1,kind:'GDAL-incomplete-proof-artifact-uploads',completed:false,uploads})+'\n', {flag:'wx'});
    await client.uploadArtifact(prefix+'-incomplete-summary', [summary], directory, {compressionLevel:0,retentionDays:days});
    console.log(JSON.stringify({completed:false,uploads}));
    return;
  }
  const raw = fs.readFileSync(indexPath);
  if (raw.length > 2*1048576) throw Error('proof index bound');
  const index = JSON.parse(raw.toString('utf8'));
  if (index.kind !== 'GDAL-remote-audit-proof-index' || index.schemaVersion !== 1 || !Array.isArray(index.parts)) throw Error('proof index schema');
  const failed = fs.existsSync(path.join(directory, 'failure.json'));
  const names = ['proof-index.json', ...index.parts.map(p => p.name), ...(failed ? ['failure.json'] : [])];
  if (new Set(names).size !== names.length || names.some(n => !/^(proof-index\.json|part-[0-9]{4}\.bin|failure\.json)$/.test(n))) throw Error('proof part names');
  const client = new DefaultArtifactClient();
  const uploads = [];
  for (const [i, name] of names.entries()) {
    const filename = path.join(directory, name), stat = fs.lstatSync(filename);
    const limit = name === 'failure.json' ? 65536 : i === 0 ? 2*1048576 : 24*1048576-65536;
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size > limit) throw Error('bounded regular proof part');
    if (i > 0 && name !== 'failure.json') {
      const declared = index.parts[i-1];
      const digest = crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex');
      if (!Number.isSafeInteger(declared.bytes) || declared.bytes !== stat.size || declared.sha256 !== digest) throw Error('proof part identity');
    }
    const artifactName = prefix + (name === 'failure.json' ? '-incomplete-failure' : i === 0 ? '-index' : '-'+name.slice(0,-4));
    const result = await client.uploadArtifact(artifactName, [filename], directory, {compressionLevel:0,retentionDays:days});
    uploads.push({file:name, artifactName, result:{id:result.id,size:result.size,digest:result.digest}});
  }
  const summary = path.join(directory, 'artifact-uploads.json');
  fs.writeFileSync(summary, JSON.stringify({schemaVersion:1,kind:'GDAL-actual-proof-artifact-uploads',completed:!failed,uploads},null,2)+'\n', {flag:'wx'});
  const result = await client.uploadArtifact(prefix+'-uploads', [summary], directory, {compressionLevel:0,retentionDays:days});
  console.log(JSON.stringify({uploads,summary:result}));
}
main().catch(error => { console.error('proof upload failed: '+error.name); process.exitCode=1; });

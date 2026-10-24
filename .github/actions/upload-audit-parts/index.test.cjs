'use strict';

// Transport-only tests. Fake SDK replies are not GitHub or Windows evidence.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const uploader = require('./index.cjs');

const sha = data => crypto.createHash('sha256').update(data).digest('hex');
const checkout = 'a'.repeat(40);
const head = 'b'.repeat(40);

function environment(root) {
  return {
    INPUT_NAME: 'profile-evidence-combined-windows-amd64', INPUT_PATH: 'public/**\n!public/excluded/**',
    'INPUT_ARTIFACT-ID': '123', 'INPUT_ARTIFACT-DIGEST': 'c'.repeat(64), 'INPUT_EXPECTED-HEAD': head,
    GITHUB_WORKSPACE: root, RUNNER_TEMP: root, GITHUB_SHA: checkout,
    GITHUB_RUN_ID: '42', GITHUB_RUN_ATTEMPT: '2', GITHUB_JOB: 'combined-windows', GITHUB_REPOSITORY: 'unit/example'
  };
}

function temporary(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'audit-uploader-unit-')));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  return root;
}

function git(command, args) {
  assert.equal(command, 'git');
  if (args[0] === 'rev-parse') return checkout + '\n';
  assert.deepEqual(args, ['cat-file', '-p', 'HEAD']);
  return `tree ${'d'.repeat(40)}\nparent ${head}\nparent ${'e'.repeat(40)}\n\nunit message\n`;
}

function metadata(root) {
  const env = environment(root);
  return uploader.sourceMetadata(uploader.inputs(env), env, root, git);
}

function plan(t, options = {}) {
  const root = temporary(t);
  const output = path.join(root, 'parts'); fs.mkdirSync(output);
  const identity = metadata(root);
  // Arbitrary unit transport bytes, not an executable, ZIP, engine or runtime.
  const blocks = options.small ? [] : [Buffer.alloc(uploader.PART_BYTES, 1), Buffer.alloc(128 * 1024, 2)];
  const parts = [];
  const whole = crypto.createHash('sha256');
  let offset = 0;
  for (let i = 0; i < blocks.length; ++i) {
    const name = `part-${String(i + 1).padStart(3, '0')}.bin`;
    fs.writeFileSync(path.join(output, name), blocks[i]);
    parts.push({index: i + 1, name, offset, bytes: blocks[i].length, sha256: sha(blocks[i])});
    offset += blocks[i].length; whole.update(blocks[i]);
  }
  const stored = {
    schemaVersion: 1, exported: !options.small, metadata: identity,
    publicAllowlist: identity.allowlist,
    transport: {format: 'zip', contentIdentity: 'workspace-relative-repacked-files', originalArtifactZipBytesPreserved: false},
    partSizeBytes: uploader.PART_BYTES,
    whole: {bytes: options.small ? 200 : offset, sha256: whole.digest('hex')}, parts,
    zipMembers: [{path: 'public/unit.bin', bytes: offset, sha256: sha(Buffer.from('unit'))}],
    collector: {path: 'verification/package-audit-parts.py', sha256: 'f'.repeat(64)}
  };
  const manifest = path.join(output, 'manifest.json');
  fs.writeFileSync(manifest, JSON.stringify(stored, null, 2) + '\n');
  const runtime = {...stored, manifest, manifestSha256: sha(fs.readFileSync(manifest)),
    parts: parts.map(part => ({...part, path: path.join(output, part.name)}))};
  return {root, output, identity, stored, runtime};
}

test('source identity supports the PR head parent of a shallow merge checkout', () => {
  const env = environment('/unit');
  const result = uploader.sourceMetadata(uploader.inputs(env), env, '/unit', git);
  assert.equal(result.headSha, head);
  assert.equal(result.checkoutSha, checkout);
  assert.equal(result.run.githubSha, checkout);
  assert.deepEqual(result.checkoutParents, [head, 'e'.repeat(40)]);
  assert.deepEqual(result.allowlist, ['public/**', '!public/excluded/**']);
  env['INPUT_EXPECTED-HEAD'] = '9'.repeat(40);
  assert.throws(() => uploader.sourceMetadata(uploader.inputs(env), env, '/unit', git), /direct merge parent/);
});

test('invalid original identities and workflow SHA fail before collection', () => {
  for (const [name, value] of [['INPUT_ARTIFACT-ID', '1e3'], ['INPUT_ARTIFACT-DIGEST', 'bad'], ['INPUT_INCLUDE-HIDDEN-FILES', 'yes']]) {
    const env = {...environment('/unit'), [name]: value};
    assert.throws(() => uploader.inputs(env));
  }
  const env = {...environment('/unit'), GITHUB_SHA: '9'.repeat(40)};
  assert.throws(() => uploader.sourceMetadata(uploader.inputs(env), env, '/unit', git), /event SHA/);
});

test('every physical raw part and the whole digest are verified before upload', async t => {
  const fixture = plan(t);
  assert.deepEqual(await uploader.validatePlan(fixture.runtime, fixture.identity, fixture.output), fixture.stored);
  const handle = fs.openSync(fixture.runtime.parts[1].path, 'r+');
  fs.writeSync(handle, Buffer.from([9]), 0, 1, 0); fs.closeSync(handle);
  await assert.rejects(uploader.validatePlan(fixture.runtime, fixture.identity, fixture.output), /part bytes/);
});

test('stdout substitution, wrong part size and more than 100 parts are rejected', async t => {
  const fixture = plan(t, {small: true});
  await assert.rejects(uploader.validatePlan({...fixture.runtime, partSizeBytes: uploader.DOWNLOAD_LIMIT}, fixture.identity, fixture.output), /transport policy/);
  await assert.rejects(uploader.validatePlan({...fixture.runtime, whole: {...fixture.runtime.whole, bytes: 300}}, fixture.identity, fixture.output), /physical manifest/);
  await assert.rejects(uploader.validatePlan({...fixture.runtime, parts: Array(101).fill({})}, fixture.identity, fixture.output), /Too many/);
});

test('parts upload sequentially and the index is last, with distinct raw and ZIP hashes', async t => {
  const fixture = plan(t);
  const stored = await uploader.validatePlan(fixture.runtime, fixture.identity, fixture.output);
  const calls = [];
  let active = false;
  const client = {async uploadArtifact(name, files, root, options) {
    assert.equal(active, false); active = true;
    calls.push({name, files, root, options});
    await new Promise(resolve => setImmediate(resolve)); active = false;
    return {id: 100 + calls.length, size: 1000, digest: String(calls.length).repeat(64)};
  }};
  const result = await uploader.uploadPlan(fixture.runtime, stored, fixture.identity, client, fixture.root, 7);
  assert.equal(calls.length, 3);
  assert.match(calls[0].name, /^audit-parts--.*--part-001$/);
  assert.match(calls[1].name, /--part-002$/);
  assert.match(calls[2].name, /--index$/);
  assert.deepEqual(calls[0].options, {compressionLevel: 0, retentionDays: 7});
  const index = JSON.parse(fs.readFileSync(calls[2].files.find(p => p.endsWith('upload-index.json')), 'utf8'));
  assert.equal(index.metadata.source.artifactDigest, 'c'.repeat(64));
  assert.equal(index.parts[0].sha256, stored.parts[0].sha256);
  assert.equal(index.parts[0].artifact.digest, '1'.repeat(64));
  assert.equal(index.parts[0].artifact.id, 101);
  assert.equal(index.originalArtifactZipBytesPreserved, false);
  assert.equal(sha(fs.readFileSync(calls[2].files.find(p => p.endsWith('manifest.json')))), fixture.runtime.manifestSha256);
  assert.equal(result.id, 103);
});

test('failed SDK upload never publishes a completion index or exposes its error body', async t => {
  const fixture = plan(t);
  const calls = [];
  const client = {async uploadArtifact(name) {
    calls.push(name);
    throw new Error('unit-only sensitive URL or runtime value must not be echoed');
  }};
  await assert.rejects(uploader.uploadPlan(fixture.runtime, fixture.stored, fixture.identity, client, fixture.root),
    error => error.message === 'Official artifact upload failed for bounded part 1');
  assert.equal(calls.length, 1);
  assert.ok(calls.every(name => !name.endsWith('--index')));
});

test('an outer artifact ZIP larger than 24MiB stops before the index', async t => {
  const fixture = plan(t);
  const calls = [];
  const client = {async uploadArtifact(name) {
    calls.push(name); return {id: 1, digest: '1'.repeat(64), size: uploader.DOWNLOAD_LIMIT + 1};
  }};
  await assert.rejects(uploader.uploadPlan(fixture.runtime, fixture.stored, fixture.identity, client, fixture.root), /24MiB/);
  assert.equal(calls.length, 1);
});

test('job-wide ledger reserves originals and indexes and refuses a fifth source', t => {
  const root = temporary(t);
  const value = metadata(root);
  let state;
  for (let i = 1; i <= 4; ++i) {
    state = uploader.reserveJobBudget(root, {...value, source: {...value.source, artifactId: i}}, 100, true);
  }
  assert.equal(state.artifacts, 408);
  assert.throws(() => uploader.reserveJobBudget(root, {...value, source: {...value.source, artifactId: 5}}, 100, true), /Too many/);
});

test('small archives retain original glob selection and skip SDK loading entirely', async t => {
  const root = temporary(t);
  fs.mkdirSync(path.join(root, 'public'));
  const source = path.join(root, 'public', 'unit.txt'); fs.writeFileSync(source, 'public unit bytes');
  const env = environment(root);
  env.GITHUB_OUTPUT = path.join(root, 'outputs');
  const dependencies = {
    glob: {async create(patterns, options) {
      assert.equal(patterns, 'public/**\n!public/excluded/**');
      assert.deepEqual(options, {followSymbolicLinks: true, implicitDescendants: true,
        omitBrokenSymbolicLinks: true, excludeHiddenFiles: true});
      return {async glob() { return [source]; }};
    }},
    execute(command, args) {
      if (command === 'git') return git(command, args);
      const readArg = name => args[args.indexOf(name) + 1];
      const output = readArg('--output'); fs.mkdirSync(output);
      const identity = JSON.parse(fs.readFileSync(readArg('--metadata'), 'utf8'));
      assert.deepEqual(JSON.parse(fs.readFileSync(readArg('--file-list'), 'utf8')), ['public/unit.txt']);
      const stored = {schemaVersion: 1, exported: false, metadata: identity, publicAllowlist: identity.allowlist,
        partSizeBytes: uploader.PART_BYTES, whole: {bytes: 200, sha256: 'f'.repeat(64)}, parts: [], zipMembers: [],
        transport: {format: 'zip', originalArtifactZipBytesPreserved: false}};
      const manifest = path.join(output, 'manifest.json'); fs.writeFileSync(manifest, JSON.stringify(stored));
      return JSON.stringify({...stored, manifest, manifestSha256: sha(fs.readFileSync(manifest))});
    }
  };
  Object.defineProperty(dependencies, 'client', {get() { throw new Error('SDK must stay unloaded'); }});
  const result = await uploader.main(env, dependencies);
  assert.equal(result.exported, 'false');
  assert.equal(result['part-count'], '0');
  assert.match(fs.readFileSync(env.GITHUB_OUTPUT, 'utf8'), /exported=false/);
});

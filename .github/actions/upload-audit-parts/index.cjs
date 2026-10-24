'use strict';

// The real local Node action receives its runtime authentication from the
// runner. Only the official artifact SDK reads or uses that authentication.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const childProcess = require('node:child_process');
const {isDeepStrictEqual} = require('node:util');

const DOWNLOAD_LIMIT = 24 * 1024 * 1024;
const PART_BYTES = DOWNLOAD_LIMIT - 64 * 1024;
const MAX_PARTS = 100;
const MAX_SOURCES_PER_JOB = 4;
const MAX_JOB_ARTIFACTS = 500;
const SHA256 = /^[0-9a-f]{64}$/;
const SHA1 = /^[0-9a-f]{40}$/;

function requireValue(condition, message) {
  if (!condition) throw new Error(message);
}

function inputs(environment) {
  function get(name, fallback = '') {
    return String(environment[`INPUT_${name.toUpperCase()}`] ?? fallback).trim();
  }
  const value = {
    name: get('name'), patterns: get('path').split(/\r?\n/).map(s => s.trim()).filter(Boolean),
    artifactId: Number(get('artifact-id')), artifactDigest: get('artifact-digest').toLowerCase(),
    expectedHead: get('expected-head').toLowerCase(), python: get('python', 'python'),
    retentionDays: Number(get('retention-days', '0')), includeHidden: get('include-hidden-files', 'false')
  };
  requireValue(value.name && value.name.length <= 255 && !/[\x00-\x1f\x7f]/.test(value.name), 'Invalid original artifact name');
  requireValue(value.patterns.length > 0 && value.patterns.every(s => !/[\x00-\x1f\x7f]/.test(s)), 'Invalid original upload patterns');
  requireValue(/^[1-9][0-9]*$/.test(get('artifact-id')) && Number.isSafeInteger(value.artifactId) && value.artifactId > 0 && SHA256.test(value.artifactDigest), 'Original artifact ID and digest are required');
  requireValue(SHA1.test(value.expectedHead), 'Expected source head is required');
  requireValue(Number.isSafeInteger(value.retentionDays) && value.retentionDays >= 0, 'Invalid artifact retention');
  requireValue(['true', 'false'].includes(value.includeHidden), 'Invalid hidden-file selection');
  requireValue(value.python && !/[\x00\r\n]/.test(value.python), 'Invalid Python executable');
  value.includeHidden = value.includeHidden === 'true';
  return value;
}

function sourceMetadata(value, environment, workspace, execute = childProcess.execFileSync) {
  function git(args) {
    try {
      return execute('git', args, {cwd: workspace, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']}).trim();
    } catch {
      throw new Error('Could not verify the current checkout identity');
    }
  }
  const checkoutSha = git(['rev-parse', 'HEAD']).toLowerCase();
  const commit = git(['cat-file', '-p', 'HEAD']).split(/\r?\n\r?\n/, 1)[0];
  const checkoutParents = commit.split(/\r?\n/).filter(s => s.startsWith('parent ')).map(s => s.slice(7));
  const githubSha = String(environment.GITHUB_SHA || '').toLowerCase();
  requireValue(SHA1.test(checkoutSha) && checkoutParents.every(s => SHA1.test(s)), 'Invalid current checkout identity');
  requireValue(checkoutSha === value.expectedHead || checkoutParents.includes(value.expectedHead), 'Expected head is not the checkout or its direct merge parent');
  requireValue(SHA1.test(githubSha) && (githubSha === checkoutSha || value.expectedHead === checkoutSha), 'Workflow event SHA differs from the verified checkout');
  requireValue(/^[1-9][0-9]*$/.test(environment.GITHUB_RUN_ID || '') && /^[1-9][0-9]*$/.test(environment.GITHUB_RUN_ATTEMPT || ''), 'Workflow run and attempt are required');
  requireValue(/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(environment.GITHUB_REPOSITORY || ''), 'Workflow repository is required');
  requireValue(/^[A-Za-z0-9_-]+$/.test(environment.GITHUB_JOB || ''), 'Workflow job is required');
  return {
    schemaVersion: 1, headSha: value.expectedHead, checkoutSha, checkoutParents,
    source: {artifactName: value.name, artifactId: value.artifactId, artifactDigest: value.artifactDigest},
    run: {id: environment.GITHUB_RUN_ID, attempt: environment.GITHUB_RUN_ATTEMPT,
      job: environment.GITHUB_JOB, repository: environment.GITHUB_REPOSITORY, githubSha},
    allowlist: value.patterns
  };
}

function ordinary(root, candidate) {
  const relative = path.relative(root, candidate);
  requireValue(relative && !path.isAbsolute(relative) && relative.split(path.sep).every(s => s !== '..'), 'Audit plan leaves its owned staging directory');
  let current = root;
  for (const component of ['', ...relative.split(path.sep)]) {
    if (component) current = path.join(current, component);
    requireValue(!fs.lstatSync(current).isSymbolicLink(), 'Audit staging must not traverse links');
    requireValue(fs.realpathSync(current) === path.resolve(current), 'Audit staging must not traverse reparse points');
  }
  requireValue(fs.statSync(candidate).isFile(), 'Audit part must be an ordinary staged file');
  return candidate;
}

async function fileDigest(filename, additionalHash) {
  const digest = crypto.createHash('sha256');
  let bytes = 0;
  for await (const block of fs.createReadStream(filename)) {
    digest.update(block);
    if (additionalHash) additionalHash.update(block);
    bytes += block.length;
  }
  return {bytes, sha256: digest.digest('hex')};
}

async function validatePlan(plan, metadata, output) {
  requireValue(plan && plan.schemaVersion === 1 && typeof plan.exported === 'boolean', 'Invalid collector plan');
  requireValue(isDeepStrictEqual(plan.metadata, metadata) && plan.partSizeBytes === PART_BYTES, 'Collector plan differs from source or transport policy');
  requireValue(plan.transport?.format === 'zip' && plan.transport.originalArtifactZipBytesPreserved === false, 'Audit repack must remain distinct from the original artifact ZIP');
  requireValue(plan.whole && Number.isSafeInteger(plan.whole.bytes) && plan.whole.bytes > 0 && SHA256.test(plan.whole.sha256), 'Invalid whole audit archive identity');
  const manifest = ordinary(output, path.resolve(plan.manifest || ''));
  const manifestIdentity = await fileDigest(manifest);
  requireValue(SHA256.test(plan.manifestSha256) && manifestIdentity.sha256 === plan.manifestSha256, 'Collector manifest digest differs');
  const stored = JSON.parse(fs.readFileSync(manifest, 'utf8'));
  requireValue(Array.isArray(plan.parts) && plan.parts.length <= MAX_PARTS, 'Too many audit parts');
  const projected = {...plan};
  delete projected.manifest;
  delete projected.manifestSha256;
  projected.parts = plan.parts.map(({path: unused, ...part}) => part);
  requireValue(isDeepStrictEqual(projected, stored), 'Collector stdout differs from its physical manifest');
  if (!plan.exported) {
    requireValue(plan.whole.bytes < DOWNLOAD_LIMIT && plan.parts.length === 0, 'Invalid skipped audit export');
    return stored;
  }
  requireValue(plan.whole.bytes >= DOWNLOAD_LIMIT && plan.whole.bytes <= MAX_PARTS * PART_BYTES,
    'Audit archive exceeds its bounded transport budget');
  requireValue(plan.parts.length === Math.ceil(plan.whole.bytes / PART_BYTES), 'Incomplete audit part inventory');
  const whole = crypto.createHash('sha256');
  let offset = 0;
  for (let i = 0; i < plan.parts.length; ++i) {
    const part = plan.parts[i];
    requireValue(part.index === i + 1 && part.name === `part-${String(i + 1).padStart(3, '0')}.bin`, 'Invalid audit part order or name');
    requireValue(part.offset === offset && part.bytes === Math.min(PART_BYTES, plan.whole.bytes - offset) && SHA256.test(part.sha256), 'Invalid audit part offset or size');
    requireValue(path.resolve(part.path || '') === path.join(output, part.name), 'Audit part path differs from its manifest name');
    const physical = await fileDigest(ordinary(output, path.resolve(part.path)), whole);
    requireValue(physical.bytes === part.bytes && physical.sha256 === part.sha256, 'Audit part bytes differ from the collector plan');
    offset += part.bytes;
  }
  requireValue(offset === plan.whole.bytes && whole.digest('hex') === plan.whole.sha256, 'Reassembled audit archive digest differs');
  return stored;
}

function reserveJobBudget(directory, metadata, partCount, exported) {
  requireValue(MAX_SOURCES_PER_JOB * (MAX_PARTS + 2) <= MAX_JOB_ARTIFACTS, 'Invalid job artifact budget policy');
  const key = crypto.createHash('sha256').update(JSON.stringify(metadata.run)).digest('hex');
  const filename = path.join(directory, `job-${key}.json`);
  if (fs.existsSync(filename)) ordinary(directory, filename);
  const state = fs.existsSync(filename) ? JSON.parse(fs.readFileSync(filename, 'utf8')) : {schemaVersion: 1, run: metadata.run, sources: [], artifacts: 0};
  requireValue(state.schemaVersion === 1 && isDeepStrictEqual(state.run, metadata.run) && Array.isArray(state.sources), 'Invalid job artifact budget receipt');
  requireValue(state.sources.length < MAX_SOURCES_PER_JOB && !state.sources.includes(metadata.source.artifactId), 'Too many or duplicate source artifact exports in this job');
  const count = 1 + (exported ? partCount + 1 : 0); // Include the preserved original and final index.
  requireValue(Number.isSafeInteger(state.artifacts) && state.artifacts >= 0 && state.artifacts + count <= MAX_JOB_ARTIFACTS,
    'Job artifact count would exceed the service limit');
  const next = {...state, sources: [...state.sources, metadata.source.artifactId], artifacts: state.artifacts + count};
  const temporary = `${filename}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(next), {flag: 'wx'});
  fs.renameSync(temporary, filename);
  return next;
}

function artifactPrefix(metadata) {
  const label = metadata.source.artifactName.replace(/[^A-Za-z0-9_.-]/g, '-').slice(0, 100) || 'source';
  return `audit-parts--${label}--${metadata.run.id}--${metadata.run.attempt}--${metadata.source.artifactId}`;
}

function validateUpload(response) {
  requireValue(response && Number.isSafeInteger(response.id) && response.id > 0 && SHA256.test(response.digest), 'Artifact SDK returned no complete upload identity');
  requireValue(Number.isSafeInteger(response.size) && response.size > 0 && response.size <= DOWNLOAD_LIMIT, 'Uploaded artifact ZIP exceeds the 24MiB download limit');
  return {id: response.id, bytes: response.size, digest: response.digest};
}

async function uploadPlan(plan, stored, metadata, client, directory, retentionDays = 0) {
  const prefix = artifactPrefix(metadata);
  const indexDirectory = path.join(directory, 'index');
  fs.mkdirSync(indexDirectory, {recursive: false});
  const manifestCopy = path.join(indexDirectory, 'manifest.json');
  fs.copyFileSync(plan.manifest, manifestCopy, fs.constants.COPYFILE_EXCL);
  const indexPath = path.join(indexDirectory, 'upload-index.json');
  function index(records) {
    return {schemaVersion: 1, transport: 'gdal4k-audit-parts', metadata,
      originalArtifactZipBytesPreserved: false,
      collectorManifest: {name: 'manifest.json', sha256: plan.manifestSha256},
      whole: stored.whole, partSizeBytes: PART_BYTES, parts: records};
  }
  const placeholders = stored.parts.map(part => ({...part,
    artifact: {name: `${prefix}--part-${String(part.index).padStart(3, '0')}`,
      id: Number.MAX_SAFE_INTEGER, bytes: DOWNLOAD_LIMIT, digest: '0'.repeat(64)}}));
  requireValue(fs.statSync(manifestCopy).size + Buffer.byteLength(JSON.stringify(index(placeholders), null, 2)) + 1 <= PART_BYTES,
    'Audit index would exceed the bounded download size');
  const records = [];
  const options = {compressionLevel: 0, ...(retentionDays ? {retentionDays} : {})};
  for (let i = 0; i < plan.parts.length; ++i) {
    const part = plan.parts[i];
    const name = placeholders[i].artifact.name;
    let response;
    try {
      response = await client.uploadArtifact(name, [part.path], path.dirname(part.path), options);
    } catch {
      // Do not print SDK error bodies or signed storage URLs.
      throw new Error(`Official artifact upload failed for bounded part ${part.index}`);
    }
    const identity = validateUpload(response);
    requireValue(isDeepStrictEqual(await fileDigest(part.path), {bytes: part.bytes, sha256: part.sha256}), 'Staged part changed during upload');
    records.push({...stored.parts[i], artifact: {name, ...identity}});
  }
  fs.writeFileSync(indexPath, JSON.stringify(index(records), null, 2) + '\n', {flag: 'wx'});
  let response;
  try {
    response = await client.uploadArtifact(`${prefix}--index`, [manifestCopy, indexPath], indexDirectory, options);
  } catch {
    throw new Error('Official artifact upload failed for the final audit index');
  }
  return validateUpload(response);
}

async function main(environment = process.env, dependencies = {}) {
  const value = inputs(environment);
  const workspace = fs.realpathSync(environment.GITHUB_WORKSPACE || process.cwd());
  const temporary = fs.realpathSync(environment.RUNNER_TEMP || '');
  const metadata = sourceMetadata(value, environment, workspace, dependencies.execute);
  const glob = dependencies.glob || require('@actions/glob');
  const globber = await glob.create(value.patterns.join('\n'), {
    followSymbolicLinks: true, implicitDescendants: true, omitBrokenSymbolicLinks: true,
    excludeHiddenFiles: !value.includeHidden
  });
  const files = (await globber.glob()).filter(filename => !fs.statSync(filename).isDirectory()).map(filename => {
    const relative = path.relative(workspace, filename);
    requireValue(relative && !path.isAbsolute(relative) && !relative.split(path.sep).includes('..'), 'Selected audit file is outside the workspace');
    return relative.split(path.sep).join('/');
  }).sort();
  requireValue(files.length > 0, 'Original artifact has no current public files');
  const directory = path.join(temporary, 'gdal4k-audit-parts');
  fs.mkdirSync(directory, {recursive: true});
  const work = fs.mkdtempSync(path.join(directory, `${value.artifactId}-`));
  const fileList = path.join(work, 'files.json');
  const metadataFile = path.join(work, 'metadata.json');
  fs.writeFileSync(fileList, JSON.stringify(files), {flag: 'wx'});
  fs.writeFileSync(metadataFile, JSON.stringify(metadata), {flag: 'wx'});
  const output = path.join(work, 'parts');
  let plan;
  try {
    const execute = dependencies.execute || childProcess.execFileSync;
    plan = JSON.parse(execute(value.python, [path.join(workspace, 'verification/package-audit-parts.py'),
      'collect', '--workspace', workspace, '--file-list', fileList, '--output', output, '--metadata', metadataFile],
    {cwd: workspace, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe']}));
  } catch {
    throw new Error('Versioned public audit collection failed');
  }
  const stored = await validatePlan(plan, metadata, output);
  reserveJobBudget(directory, metadata, plan.parts.length, plan.exported);
  const result = {exported: String(plan.exported), 'part-count': String(plan.parts.length), 'index-artifact-id': '', 'index-artifact-digest': ''};
  if (plan.exported) {
    // Import only after all local source, file, hash and capacity checks pass.
    const client = dependencies.client || new (require('@actions/artifact').DefaultArtifactClient)();
    const response = await uploadPlan(plan, stored, metadata, client, work, value.retentionDays);
    result['index-artifact-id'] = String(response.id);
    result['index-artifact-digest'] = response.digest;
  }
  if (environment.GITHUB_OUTPUT) {
    fs.appendFileSync(environment.GITHUB_OUTPUT, Object.entries(result).map(([k, v]) => `${k}=${v}\n`).join(''));
  }
  console.log(plan.exported ? `Uploaded ${plan.parts.length} bounded audit parts and the final index.` : 'Public audit archive is below 24MiB; no additional artifact export.');
  return result;
}

module.exports = {DOWNLOAD_LIMIT, PART_BYTES, MAX_PARTS, MAX_SOURCES_PER_JOB,
  inputs, sourceMetadata, validatePlan, reserveJobBudget, uploadPlan, artifactPrefix, main};

if (require.main === module) {
  main().catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}

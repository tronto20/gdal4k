(async function runCohortRecoveryBatch(options) {
  const base = '/workspace/shared/gdal4k-c23-acceptance-tools-v1/receiver-normal-download-v1';
  const receiver = base + '/receive_cohort_raw_v1.py';
  let inputs;
  const ids = options.artifactIds;
  const sourcePin = options.orchestratorSource;
  if (!Array.isArray(ids) || ids.length < 1 || ids.length > 4 || new Set(ids).size !== ids.length || ids.some(id => !Number.isSafeInteger(id) || id <= 0)) throw new Error('one to four distinct artifact IDs required');
  if (!sourcePin || sourcePin.path !== base + '/recover_cohort_batch_v1.js' || !/^[0-9a-f]{64}$/.test(sourcePin.sha256) || !Number.isSafeInteger(sourcePin.bytes)) throw new Error('exact reviewed orchestrator source pin required');
  for (const key of ['cohortManifest', 'activation']) {
    const ref = options[key];
    if (!ref || typeof ref.path !== 'string' || !ref.path.startsWith('/') || !/^[0-9a-f]{64}$/.test(ref.sha256) || !Number.isSafeInteger(ref.bytes) || ref.bytes <= 0) throw new Error('exact pinned ' + key + ' required');
  }
  if (typeof options.sessionRoot !== 'string' || !options.sessionRoot.startsWith(base + '/sessions/')) throw new Error('new reviewed session root required');
  const binding = ['--cohort-manifest', options.cohortManifest.path, '--cohort-manifest-sha256', options.cohortManifest.sha256, '--activation', options.activation.path, '--activation-sha256', options.activation.sha256, '--session-root', options.sessionRoot];
  const sh = s => "'" + String(s).replace(/'/g, "'\\''") + "'";
  async function shell(cmd) {
    let r = await tools.exec_command({cmd, max_output_tokens: 1800, yield_time_ms: 1000});
    let output = r.output || '';
    while (r.session_id !== undefined) {
      r = await tools.write_stdin({session_id: r.session_id, chars: '', max_output_tokens: 1800, yield_time_ms: 1000});
      output += r.output || '';
    }
    if (r.exit_code !== 0) throw new Error('receiver command failed: ' + output.slice(-5000));
    return output.trim();
  }
  async function command(args) {
    const bootstrap = `import os,sys,json,hashlib,stat,resource
resource.setrlimit(resource.RLIMIT_AS,(128*1048576,128*1048576))
keys=('st_dev','st_ino','st_mode','st_nlink','st_uid','st_gid','st_size','st_atime_ns','st_mtime_ns','st_ctime_ns','st_blocks')
def read(r):
 assert type(r) is dict and set(r)=={'path','bytes','sha256'} and type(r['bytes']) is int and 0<r['bytes']<=2*1048576
 p=r['path'];assert type(p) is str and os.path.isabs(p) and os.path.normpath(p)==p
 d=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
 try:
  for part in p.split('/')[1:-1]:
   n=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=d);os.close(d);d=n
  fd=os.open(p.split('/')[-1],os.O_RDONLY|os.O_NOATIME|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=d)
 finally:os.close(d)
 with os.fdopen(fd,'rb') as f:
  s=os.fstat(f.fileno());before=tuple(getattr(s,k) for k in keys);assert stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode)==0o400 and s.st_uid==os.getuid() and s.st_nlink==1 and s.st_size==r['bytes']
  b=f.read(r['bytes']+1);assert len(b)==r['bytes'] and hashlib.sha256(b).hexdigest()==r['sha256'];assert before==tuple(getattr(os.fstat(f.fileno()),k) for k in keys)==tuple(getattr(os.lstat(p),k) for k in keys)
  return b
activation=json.loads(read(json.loads(sys.argv[1])));actor=sys.argv[2]
refs=activation.get('sources');assert type(refs) is list and len(refs)==4
selected=[r for r in refs if type(r) is dict and r.get('path')==actor];assert len(selected)==1
body=read(selected[0]);sys.argv=[actor,*sys.argv[3:]]
exec(compile(body,actor,'exec'),{'__name__':'__main__','__file__':actor})
`;
    return JSON.parse(await shell('python3 -B -c ' + sh(bootstrap) + ' ' + [JSON.stringify(options.activation), receiver].concat(binding, args).map(sh).join(' ')));
  }
  const info = await command(['batch-info']);
  if (info.cohortManifest.path !== options.cohortManifest.path || info.cohortManifest.bytes !== options.cohortManifest.bytes || info.cohortManifest.sha256 !== options.cohortManifest.sha256 || info.orchestratorSource.path !== sourcePin.path || info.orchestratorSource.bytes !== sourcePin.bytes || info.orchestratorSource.sha256 !== sourcePin.sha256 || ids.some(id => !info.selectedForNewDownload.includes(id))) throw new Error('batch differs from exact activated plan');
  inputs = info.metadataInputs;
  async function stageJSON(id, stage, value) {
    const path = inputs + '/' + id + '-' + stage + '.json';
    const body = JSON.stringify(value, null, 2) + '\n';
    const staged = await command(['stage-input', '--artifact-id', String(id), '--stage', stage, '--input-json', body]);
    if (staged.path !== path) throw new Error('unexpected receiver-owned stage path');
    return staged.path;
  }
  async function persistOperation(id, stage, operation) {
    const value = {schemaVersion: 1, kind: 'GDAL-cohort-actual-normal-download-tool-operation', artifactId: id, stage, orchestratorSource: sourcePin, operation};
    const path = await stageJSON(id, stage, value);
    return await command(['seal-operation', '--artifact-id', String(id), '--stage', stage, '--input', path]);
  }
  async function runOne(id) {
    const record = {artifactId: id, phase: 'admitting'};
    store('GDALCohortRecoveryArtifact-' + id, record);
    record.lease = await command(['admit', '--artifact-id', String(id)]);
    record.phase = 'plugin-request';
    const args = {repo_full_name: info.repository, artifact_id: id, file_name: 'gdal-cohort-artifact-' + id + '.zip'};
    const before = await tools.clock__curr_time({});
    const result = await tools.mcp__codex_apps__github_download_workflow_artifact(args);
    const after = await tools.clock__curr_time({});
    const plugin = {name: 'mcp__codex_apps__github_download_workflow_artifact', arguments: args, before, after, result};
    record.pluginTool = plugin;
    store('GDALCohortRecoveryArtifact-' + id, record);
    record.pluginObservation = await persistOperation(id, 'plugin', plugin);
    if (!result || result.isError === true || result.error) throw new Error('normal plugin error for ' + id);
    const sc = result.structuredContent;
    if (!sc || sc.artifact_id !== id || sc.file_name !== args.file_name || sc.mime_type !== 'application/zip' || !sc.file_uri || !/^file_[A-Za-z0-9_-]+$/.test(sc.file_uri.file_id)) throw new Error('unexpected actual plugin schema for ' + id);
    const fileId = sc.file_uri.file_id;
    record.phase = 'local-file-download';
    const localBefore = await tools.clock__curr_time({});
    const localArgs = {file_id: fileId};
    const localResult = await tools.download_file(localArgs);
    const localAfter = await tools.clock__curr_time({});
    const local = {name: 'download_file', arguments: localArgs, before: localBefore, after: localAfter, result: localResult};
    record.fileDownloadTool = local;
    store('GDALCohortRecoveryArtifact-' + id, record);
    record.fileDownloadObservation = await persistOperation(id, 'file', local);
    if (!localResult || localResult.isError === true || localResult.error || typeof localResult.path !== 'string' || !Number.isSafeInteger(localResult.size_bytes)) throw new Error('unexpected actual local file result for ' + id);
    const landing = localResult.path;
    const statCode = "import os,sys,json,stat; s=os.lstat(sys.argv[1]); assert stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and s.st_nlink==1; print(json.dumps({k:getattr(s,k) for k in ('st_dev','st_ino','st_mode','st_nlink','st_uid','st_gid','st_size','st_atime_ns','st_mtime_ns','st_ctime_ns','st_blocks')}))";
    // Keep exact nanosecond integers opaque across JavaScript's Number boundary.
    const landingFull11JSON = await shell('python3 -c ' + sh(statCode) + ' ' + sh(landing));
    const transport = {schemaVersion: 1, kind: 'GDAL-cohort-normal-artifact-download-observation', artifactId: id, orchestratorSource: sourcePin, pluginTool: plugin, fileDownloadTool: local, pluginObservation: record.pluginObservation, fileDownloadObservation: record.fileDownloadObservation, landingPath: landing, landingFull11JSON};
    const inputPath = await stageJSON(id, 'transport', transport);
    record.transport = await command(['seal-transport', '--artifact-id', String(id), '--input', inputPath]);
    record.phase = 'copy-hash-promote-retire';
    record.completed = await command(['receive', '--transport', record.transport.path]);
    record.phase = 'completed';
    store('GDALCohortRecoveryArtifact-' + id, record);
    notify({artifactId: id, completed: record.completed, phase: record.phase});
    return {artifactId: id, completed: record.completed};
  }
  const results = await Promise.allSettled(ids.map(runOne));
  const summary = results.map((r, i) => r.status === 'fulfilled' ? {artifactId: ids[i], status: 'fulfilled', value: r.value} : {artifactId: ids[i], status: 'rejected', error: String(r.reason)});
  store('GDALCohortRecoveryLastBatch', summary);
  text(summary);
  if (results.some(r => r.status !== 'fulfilled')) throw new Error('Recovery batch failed; retain all leases, operation receipts and partial/landing files. Do not retry or start another batch before root review.');
  return summary;
})

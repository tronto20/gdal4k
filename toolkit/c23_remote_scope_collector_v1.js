(async function collectCurrentCohortScope(options) {
  // Normal connector GET observations only. This source is prepared separately
  // from any actual invocation or subsequent catalogue/acceptance records.
  const root = '/workspace/shared/gdal4k-c23-acceptance-tools-v1';
  const sourcePath = root + '/c23_remote_scope_collector_v1.js';
  const policySource = {path: root + '/cohort_policy_v1.py', bytes: 28680,
    sha256: 'ea15892360878b258cd6ab584c7b75be49a1d591c861a7a7fc599e8258346aa5'};
  const pin = options.collectorSource;
  const manifest = options.cohortManifest;
  const directory = options.outputDirectory;
  const validPin = value => value && Object.keys(value).sort().join(',') === 'bytes,path,sha256' &&
    typeof value.path === 'string' && Number.isSafeInteger(value.bytes) && value.bytes > 0 &&
    /^[0-9a-f]{64}$/.test(value.sha256);
  if (!validPin(pin) || pin.path !== sourcePath || !validPin(manifest)) throw new Error('exact source and static intent pins required');
  if (typeof directory !== 'string' || !/^\/workspace\/shared\/gdal4k-c23-current-ci-[A-Za-z0-9_-]+$/.test(directory)) throw new Error('unique owned current scope directory required');
  const sh = value => "'" + String(value).replace(/'/g, "'\\''") + "'";
  async function command(code, args) {
    let result = await tools.exec_command({cmd: 'python3 -B -c ' + sh(code) + ' ' + args.map(sh).join(' '), max_output_tokens: 2000, yield_time_ms: 1000});
    let output = result.output || '';
    while (result.session_id !== undefined) {
      result = await tools.write_stdin({session_id: result.session_id, chars: '', max_output_tokens: 2000, yield_time_ms: 1000});
      output += result.output || '';
    }
    if (result.exit_code !== 0) throw new Error('scope source/persistence check failed: ' + output.slice(-3000));
    return output.trim();
  }
  const initialize = `import os,sys,stat,hashlib,json,types
def read(reference):
 fd=os.open(reference['path'],os.O_RDONLY|os.O_NOATIME|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(fd,'rb') as f:
  s=os.fstat(f.fileno()); keys=('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_atime_ns','st_mtime_ns','st_ctime_ns','st_blocks'); before=tuple(getattr(s,k) for k in keys)
  assert stat.S_ISREG(s.st_mode) and type(reference['bytes']) is int and 0<reference['bytes']<=2*1048576 and s.st_size==reference['bytes']
  assert s.st_uid==os.getuid() and s.st_nlink==1 and (s.st_mode&0o7777)==0o400
  b=f.read(2*1048576+1); assert len(b)==reference['bytes'] and hashlib.sha256(b).hexdigest()==reference['sha256']
  assert tuple(getattr(os.fstat(f.fileno()),k) for k in keys)==before
  assert tuple(getattr(os.lstat(reference['path']),k) for k in keys)==before
  return b
own,policy,manifest=[json.loads(v) for v in sys.argv[1:4]]
read(own); raw=read(policy); module=types.ModuleType('c23_collector_policy'); module.__file__=policy['path']; exec(compile(raw,policy['path'],'exec'),module.__dict__)
intent=module.install(manifest); assert os.statvfs('/workspace').f_bavail*os.statvfs('/workspace').f_frsize>=40*1048576
os.mkdir(sys.argv[4],0o700); fd=os.open(os.path.dirname(sys.argv[4]),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW); os.fsync(fd); os.close(fd)
print(json.dumps({'scope':intent['scope'],'ci':intent['ci']}))`;
  const intent = JSON.parse(await command(initialize, [JSON.stringify(pin), JSON.stringify(policySource), JSON.stringify(manifest), directory]));
  const scope = intent.scope;
  const api = 'https://api.github.com/repos/' + scope.repository;
  const observations = [];
  const byRole = new Map();
  const utf8Size = value => {
    let bytes = 0;
    for (const scalar of value) {
      const point = scalar.codePointAt(0);
      bytes += point < 0x80 ? 1 : point < 0x800 ? 2 : point < 0x10000 ? 3 : 4;
    }
    return bytes;
  };
  let observationBytes = 0;
  let budgetExceeded = false;
  const started = (await tools.clock__curr_time({})).current_time;
  const fetchOne = async ([role, path]) => {
    const url = api + path;
    try {
      const before = await tools.clock__curr_time({});
      const result = await tools.mcp__codex_apps__github_fetch({url});
      const after = await tools.clock__curr_time({});
      const entry = {role, url, before, after, result};
      const rendered = JSON.stringify(entry, null, 2);
      // Account for its additional four spaces inside the outer array, plus
      // separators. Reserve64KiB for the fixed header and bounded error rows.
      const charge = utf8Size(rendered) + 4 * (1 + (rendered.match(/\n/g) || []).length) + 2;
      if (observationBytes + charge > 8 * 1048576 - 65536) {
        budgetExceeded = true;
        throw new Error('observation metadata budget exceeded; full response not retained and cannot qualify');
      }
      observationBytes += charge;
      observations.push(entry); byRole.set(role, entry);
      return entry;
    } catch (error) {
      const entry = {role, url, error: String(error)};
      observations.push(entry); byRole.set(role, entry);
      return entry;
    }
  };
  const fetchBatch = async specifications => {
    for (let offset = 0; offset < specifications.length; offset += 4) {
      const results = await Promise.allSettled(specifications.slice(offset, offset + 4).map(fetchOne));
      for (const result of results) if (result.status !== 'fulfilled') throw new Error('unrecorded collector failure: ' + String(result.reason));
      if (budgetExceeded) throw new Error('collection stopped at the bounded observation metadata budget');
    }
  };
  const body = role => {
    const entry = byRole.get(role);
    if (!entry || entry.error || !entry.result || entry.result.isError || !entry.result.structuredContent ||
        entry.result.structuredContent.url !== entry.url || typeof entry.result.structuredContent.content !== 'string') throw new Error('unusable actual response for ' + role);
    return JSON.parse(entry.result.structuredContent.content);
  };
  const pages = (value, field, role) => {
    if (!value || !Number.isSafeInteger(value.total_count) || value.total_count < 0 || value.total_count > 10000 ||
        !Array.isArray(value[field]) || value[field].length !== Math.min(value.total_count, 100)) throw new Error('incomplete/oversized first page for ' + role);
    return Math.max(1, Math.ceil(value.total_count / 100));
  };
  try {
    await fetchBatch([
      ['pr', '/pulls/' + scope.pr], ['mainBranch', '/branches/main'],
      ['headCommit', '/git/commits/' + scope.head], ['baseCommit', '/git/commits/' + scope.base],
      ['checkoutCommit', '/git/commits/' + scope.testCheckout],
      ['baseHeadCompare', '/compare/' + scope.base + '...' + scope.head + '?per_page=100&page=1'],
      ['primaryRun', '/actions/runs/' + scope.primaryRun], ['profileRun', '/actions/runs/' + scope.profileRun],
      ['headBranch', '/branches/' + scope.branch], ['prFiles1', '/pulls/' + scope.pr + '/files?per_page=100&page=1']
    ]);
    const attempts = {};
    const specifications = [];
    for (const [label, runId] of [['primary', scope.primaryRun], ['profile', scope.profileRun]]) {
      const run = body(label + 'Run');
      if (run.id !== runId || run.head_sha !== scope.head || !Number.isSafeInteger(run.run_attempt) || run.run_attempt <= 0) throw new Error('current actual run/attempt identity differs');
      attempts[label] = run.run_attempt;
      specifications.push([label + 'Attempt', '/actions/runs/' + runId + '/attempts/' + run.run_attempt],
        [label + 'Jobs1', '/actions/runs/' + runId + '/attempts/' + run.run_attempt + '/jobs?per_page=100&page=1'],
        [label + 'Artifacts1', '/actions/runs/' + runId + '/artifacts?per_page=100&page=1']);
    }
    await fetchBatch(specifications);
    const remaining = [];
    for (const [label, runId] of [['primary', scope.primaryRun], ['profile', scope.profileRun]]) {
      for (const [kind, field] of [['Jobs', 'jobs'], ['Artifacts', 'artifacts']]) {
        const count = pages(body(label + kind + '1'), field, label + kind);
        const prefix = '/actions/runs/' + runId + (kind === 'Jobs' ? '/attempts/' + attempts[label] + '/jobs' : '/artifacts');
        for (let page = 2; page <= count; page++) remaining.push([label + kind + page, prefix + '?per_page=100&page=' + page]);
      }
    }
    const pr = body('pr');
    if (!Number.isSafeInteger(pr.changed_files) || pr.changed_files < 0 || pr.changed_files > 10000 ||
        !Array.isArray(body('prFiles1')) || body('prFiles1').length !== Math.min(pr.changed_files, 100)) throw new Error('incomplete/oversized initial PR file page');
    for (let page = 2; page <= Math.max(1, Math.ceil(pr.changed_files / 100)); page++) remaining.push(['prFiles' + page, '/pulls/' + scope.pr + '/files?per_page=100&page=' + page]);
    await fetchBatch(remaining);
  } catch (error) {
    // Preserve all genuine partial operations. Such a record cannot qualify.
    observations.push({role: 'collectionFailure', url: api, error: String(error)});
  }
  const ended = (await tools.clock__curr_time({})).current_time;
  const record = {schemaVersion: 1, kind: 'GDAL-cohort-actual-remote-observations', cohortManifest: manifest,
    observedStartedAtUtc: started, observedCompletedAtUtc: ended, collectorSource: pin, observations};
  const encoded = JSON.stringify(record, null, 2) + '\n';
  if (utf8Size(encoded) > 8 * 1048576) throw new Error('observation UTF-8 bytes exceed 8MiB; do not persist or qualify');
  const path = directory + '/actual-observations.json';
  const patch = '*** Begin Patch\n*** Add File: ' + path + '\n' + encoded.trimEnd().split('\n').map(line => '+' + line).join('\n') + '\n*** End Patch';
  const written = await tools.apply_patch(patch);
  if (written && written.isError) throw new Error('observation persistence tool rejected write');
  const seal = "import os,sys,hashlib,json; p=sys.argv[1]; fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME); f=os.fdopen(fd,'rb'); b=f.read(8*1048576+1); assert len(b)<=8*1048576; os.fsync(f.fileno()); os.fchmod(f.fileno(),0o400); os.fsync(f.fileno()); f.close(); d=os.open(os.path.dirname(p),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW); os.fsync(d); os.close(d); print(json.dumps(dict(path=p,bytes=len(b),sha256=hashlib.sha256(b).hexdigest())))";
  const resultPin = JSON.parse(await command(seal, [path]));
  store('GDALCohortLatestActualRemoteObservations', resultPin);
  text({observations: resultPin, operationCount: observations.length,
    errors: observations.filter(value => value.error).map(value => ({role: value.role, error: value.error})), qualifiedAcceptanceScope: false});
  return resultPin;
})

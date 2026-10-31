"""Qualify actual normal GitHub observations against pinned current cohort intent.

This preparation makes no network calls. An actual later invocation derives
current roles, attempts and catalogue identities without accepting capabilities.
"""
from __future__ import annotations
import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import cohort_policy_v1 as policy

MiB=1024**2
MAX_INPUT=8*MiB
MAX_CATALOGUE=2*MiB
MAX_OUTPUT=24*MiB
RSS_LIMIT=128*MiB
MAX_PAGES=100
MAX_ROWS=100*MAX_PAGES
COLLECTOR_REF={'path': '/workspace/shared/gdal4k-c23-acceptance-tools-v1/c23_remote_scope_collector_v1.js', 'bytes': 10599, 'sha256': 'b81ece9d841703315cceb5b775733ff46dff8eaa2190c46ca74e7b260b9dee54'}
POLICY_REF={'path': '/workspace/shared/gdal4k-c23-acceptance-tools-v1/cohort_policy_v1.py', 'bytes': 28680, 'sha256': 'ea15892360878b258cd6ab584c7b75be49a1d591c861a7a7fc599e8258346aa5'}

class ScopeError(ValueError):pass
def need(value,message):
    if not value:raise ScopeError(message)
def full11(s):
    return {k:getattr(s,k) for k in ['st_dev','st_ino','st_mode','st_nlink','st_uid','st_gid','st_size','st_atime_ns','st_mtime_ns','st_ctime_ns','st_blocks']}
def rss():
    with open('/proc/self/status',encoding='ascii') as f:text=f.read(16384)
    found=re.search(r'^VmHWM:\s+([0-9]+) kB$',text,re.M)
    need(found is not None,'Process peak RSS is unavailable')
    value=int(found[1])*1024;need(value<=RSS_LIMIT,'Qualifier peak RSS exceeds128MiB');return value
def unique(pairs):
    out={}
    for k,v in pairs:need(k not in out,'Duplicate JSON object key');out[k]=v
    return out
def finite_float(text):
    value=float(text);need(math.isfinite(value),'Nonfinite JSON number');return value
def strict_json(raw):
    need(isinstance(raw,(bytes,str)),'JSON input is not bytes/text')
    need(len(raw.encode('utf-8') if isinstance(raw,str) else raw)<=MAX_INPUT,'JSON input exceeds8MiB')
    def invalid(text):raise ScopeError('Nonfinite JSON constant:'+text)
    return json.loads(raw,object_pairs_hook=unique,parse_constant=invalid,parse_float=finite_float)
def clock(text):
    need(isinstance(text,str),'Clock timestamp is not text')
    try:value=dt.datetime.strptime(text,'%Y-%m-%d %H:%M:%S UTC').replace(tzinfo=dt.timezone.utc)
    except ValueError as e:raise ScopeError('Invalid actual clock timestamp') from e
    need(value.strftime('%Y-%m-%d %H:%M:%S UTC')==text,'Noncanonical clock timestamp');return value
def iso(value):return value.isoformat()
def canonical(value):return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()

def github_content(result,expected_url):
    need(isinstance(result,dict) and set(result)<={'content','structuredContent','isError','_meta'} and result.get('isError',False) is False,'Actual GitHub tool operation failed or has unexpected error/HTTP wrapper fields')
    need(result.get('_meta') in (None,{}),'Unsupported nonempty tool metadata; possible nested error wrapper')
    need(isinstance(result.get('content'),list),'Actual MCP content blocks are missing')
    for block in result['content']:
        need(isinstance(block,dict) and set(block)<={'type','text','annotations','_meta'} and block.get('type')=='text' and isinstance(block.get('text'),str) and block.get('_meta') in (None,{}),'Unexpected MCP acknowledgement/error block')
    value=result.get('structuredContent')
    need(isinstance(value,dict) and value.get('url')==expected_url and isinstance(value.get('content'),str),'Actual structured GitHub URL/content differs')
    basic={'url','content'}
    rich=basic|{'title','modified_date','display_url','display_title','structuredContent'}
    need(set(value) in (basic,rich),'Unexpected GitHub response wrapper schema')
    if set(value)==rich:
        nested=value['structuredContent']
        need(isinstance(nested,dict) and set(nested)=={'title','content','display_url'} and nested['content']==value['content'] and nested['title']==value['title'] and nested['display_url']==value['display_url'],'Conflicting actual outer/nested GitHub response body/title/display URL')
        need(all(value[k] is None or isinstance(value[k],str) for k in ['title','modified_date','display_url','display_title']),'Invalid GitHub response presentation metadata')
    return value['content']

def open_parent(path):
    path=Path(path);need(path.is_absolute() and '..' not in path.parts,'Absolute canonical path required')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for name in path.parts[1:-1]:
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
        return fd,path.name
    except BaseException:os.close(fd);raise
def read_pin(ref,limit=MAX_INPUT,*,sealed=False):
    need(isinstance(ref,dict) and set(ref)=={'path','bytes','sha256'},'Invalid file reference schema')
    need(type(ref['bytes']) is int and 0<=ref['bytes']<=limit and isinstance(ref['sha256'],str) and re.fullmatch('[0-9a-f]{64}',ref['sha256']),'Invalid file reference byte identity')
    parent,name=open_parent(ref['path'])
    try:fd=os.open(name,os.O_RDONLY|os.O_NOATIME|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    finally:os.close(parent)
    with os.fdopen(fd,'rb') as f:
        before=full11(os.fstat(f.fileno()));need(stat.S_ISREG(before['st_mode']) and before['st_size']==ref['bytes'],'Pinned input type/length differs')
        if sealed:need(before['st_uid']==os.getuid() and before['st_nlink']==1 and stat.S_IMODE(before['st_mode'])==0o400,'Pinned actual input is not owned0400/nlink1')
        raw=f.read(ref['bytes']+1)
        need(len(raw)==ref['bytes'] and hashlib.sha256(raw).hexdigest()==ref['sha256'],'Pinned input whole size/SHA differs')
        need(full11(os.fstat(f.fileno()))==before and full11(os.lstat(ref['path']))==before,'Pinned input full11 changed')
    rss();return raw
def reference(path,sha=None,limit=MAX_INPUT):
    s=os.lstat(path);need(stat.S_ISREG(s.st_mode) and s.st_size<=limit,'Input file size/type exceeds bound')
    if sha is None:
        parent,name=open_parent(path)
        try:fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME|os.O_NONBLOCK,dir_fd=parent)
        finally:os.close(parent)
        with os.fdopen(fd,'rb') as f:b=f.read(limit+1)
        need(len(b)==s.st_size and len(b)<=limit,'Input length changed');sha=hashlib.sha256(b).hexdigest()
    return {'path':str(path),'bytes':s.st_size,'sha256':sha}


def timestamp(value):
    need(type(value) is str and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z',value),
         'Noncanonical GitHub UTC timestamp')
    try:return dt.datetime.fromisoformat(value[:-1]+'+00:00')
    except ValueError as error:raise ScopeError('Invalid GitHub timestamp') from error


def require_intent_reference(value):
    try:return policy.require_intent_reference(value)
    except policy.IntentError as error:raise ScopeError(str(error)) from error


def validate_reference(value,maximum=MAX_INPUT):
    try:return policy.validate_reference(value,maximum)
    except policy.IntentError as error:raise ScopeError(str(error)) from error


def check_run(row,run_id,plan,expected_attempt=None):
    scope=plan['scope'];head,tree=scope['head'],scope['tree'];repo_id=scope['repositoryId'];repo=scope['repository']
    need(type(row) is dict and type(row.get('id')) is int and row['id']==run_id and
         type(row.get('run_attempt')) is int and row['run_attempt']>0,'Run/attempt identity differs')
    if expected_attempt is not None:need(row['run_attempt']==expected_attempt,'Latest run and actual attempt differ')
    need(row['head_sha']==head and row['head_branch']==scope['branch'] and row['event']=='pull_request' and
         row['status']=='completed' and row['conclusion']=='success','Run source or successful completion differs')
    path='.github/workflows/build-and-publish.yml' if run_id==scope['primaryRun'] else '.github/workflows/driver-profiles.yml'
    need(path in {ref['path'] for ref in plan['ci']['workflowSourceRefs']} and row['path']==path and
         row['head_commit']['id']==head and row['head_commit']['tree_id']==tree,'Run workflow/source commit/tree differs')
    for key in ('repository','head_repository'):
        need(type(row[key]['id']) is int and row[key]['id']==repo_id and row[key]['full_name']==repo,'Run repository identity differs')
    prs=row['pull_requests']
    need(type(prs) is list and len(prs)==1 and type(prs[0]['number']) is int and prs[0]['number']==scope['pr'] and
         prs[0]['head']['sha']==head and prs[0]['base']['sha']==scope['base'],'Run pull-request source/base differs')
    for key in ('head','base'):need(type(prs[0][key]['repo']['id']) is int and prs[0][key]['repo']['id']==repo_id,'Run PR repository differs')
    referenced=row['referenced_workflows']
    if run_id==scope['primaryRun']:
        need(type(referenced) is list and len(referenced)==1 and referenced[0]['sha']==scope['testCheckout'] and
             referenced[0]['path']==repo+'/.github/workflows/library-tests.yml@'+scope['testCheckout'] and
             referenced[0]['ref']==f"refs/pull/{scope['pr']}/merge",'Primary reusable workflow checkout differs')
    else:need(referenced==[],'Unexpected profile referenced workflow')
    timestamp(row['run_started_at'])
    return row['run_attempt']


def artifact(item,plan,attempt_starts,end):
    scope=plan['scope'];api='https://api.github.com/repos/'+scope['repository']
    need(type(item) is dict and all(key in item for key in ('workflow_run','id','size_in_bytes','digest','name','expired','url','archive_download_url','created_at')),
         'Artifact fields are incomplete')
    run=item['workflow_run'];aid=item['id'];size=item['size_in_bytes'];digest=item['digest']
    need(type(run) is dict and type(aid) is int and aid>0 and type(size) is int and size>0,'Artifact ID/size must be positive integers')
    need(type(item['name']) is str and 0<len(item['name'])<=512 and type(digest) is str and re.fullmatch('sha256:[0-9a-f]{64}',digest),
         'Artifact name/digest invalid')
    need(item['expired'] is False and type(run.get('id')) is int and run['id'] in attempt_starts and
         run['head_sha']==scope['head'] and type(run['repository_id']) is int and run['repository_id']==scope['repositoryId'] and
         type(run['head_repository_id']) is int and run['head_repository_id']==scope['repositoryId'] and run['head_branch']==scope['branch'],
         'Artifact expired or run/source repository/head differs')
    created=timestamp(item['created_at'])
    need(attempt_starts[run['id']]<=created<=end,'Artifact does not prove creation inside the current actual attempt')
    need(item['url']==api+f'/actions/artifacts/{aid}' and item['archive_download_url']==api+f'/actions/artifacts/{aid}/zip',
         'Artifact canonical service URLs differ')
    return {'id':aid,'name':item['name'],'size':size,'digest':digest,'runId':run['id'],'head':scope['head'],'expired':False}


def _pages(bodies,expected_urls,label,kind,field,prefix):
    first=bodies.get(label+kind+'1')
    need(type(first) is dict and type(first.get('total_count')) is int and 0<=first['total_count']<=MAX_ROWS,
         'Missing/invalid first page total count')
    total=first['total_count'];count=max(1,(total+99)//100);rows=[]
    for page in range(1,count+1):
        role=label+kind+str(page);expected_urls[role]=prefix+f'?per_page=100&page={page}'
        body=bodies.get(role);wanted=min(100,max(0,total-100*(page-1)))
        need(type(body) is dict and type(body.get('total_count')) is int and body['total_count']==total and
             type(body.get(field)) is list and len(body[field])==wanted,'Incomplete/extra/inconsistent actual list pagination')
        rows.extend(body[field])
    need(len(rows)==total,'Actual pagination total differs')
    return rows


def qualify_record(record,catalogue,now):
    """Pure derivation from actual responses; synthetic tests are labeled separately."""
    plan=policy.current();scope=plan['scope'];intent_ref=policy.current_ref();api='https://api.github.com/repos/'+scope['repository']
    age=plan['ci']['maximumAgeSeconds'];head,tree,base=scope['head'],scope['tree'],scope['base']
    expected_urls={'pr':api+f"/pulls/{scope['pr']}",'mainBranch':api+'/branches/main',
        'headCommit':api+'/git/commits/'+head,'baseCommit':api+'/git/commits/'+base,
        'checkoutCommit':api+'/git/commits/'+scope['testCheckout'],'headBranch':api+'/branches/'+scope['branch'],
        'baseHeadCompare':api+'/compare/'+base+'...'+head+'?per_page=100&page=1',
        'primaryRun':api+f"/actions/runs/{scope['primaryRun']}",'profileRun':api+f"/actions/runs/{scope['profileRun']}"}
    need(type(record) is dict and set(record)=={'schemaVersion','kind','cohortManifest','observedStartedAtUtc','observedCompletedAtUtc','collectorSource','observations'},
         'Unexpected actual observation record schema')
    require_intent_reference(record['cohortManifest'])
    need(type(record['schemaVersion']) is int and record['schemaVersion']==1 and
         record['kind']=='GDAL-cohort-actual-remote-observations' and record['cohortManifest']==intent_ref,
         'Actual observation static intent/kind/version differs')
    validate_reference(record['collectorSource'],MiB)
    need(record['collectorSource']==COLLECTOR_REF,'Actual observation collector source differs')
    begin,end=clock(record['observedStartedAtUtc']),clock(record['observedCompletedAtUtc'])
    need(now.tzinfo is not None and begin<=end<=now and 0<=(now-begin).total_seconds()<=age,
         'Actual observation interval is future/stale/reversed')
    observations=record['observations'];need(type(observations) is list and 1<=len(observations)<=len(expected_urls)+5*MAX_PAGES+2,'Actual operation count exceeds bounded pagination')
    bodies={};entries={}
    for entry in observations:
        need(type(entry) is dict and set(entry)=={'role','url','before','after','result'},'Observation missing actual result/clock fields or has extra fields')
        role=entry['role'];need(type(role) is str and re.fullmatch(r'(?:pr|mainBranch|headCommit|baseCommit|checkoutCommit|baseHeadCompare|headBranch|(?:primary|profile)(?:Run|Attempt|Jobs[1-9][0-9]*|Artifacts[1-9][0-9]*)|prFiles[1-9][0-9]*)',role) and role not in bodies,
             'Observation role is invalid or duplicated')
        need(type(entry['before']) is dict and set(entry['before'])=={'current_time'} and type(entry['after']) is dict and set(entry['after'])=={'current_time'},
             'Actual clock tool result schema differs')
        before,after=clock(entry['before']['current_time']),clock(entry['after']['current_time'])
        need(begin<=before<=after<=end,'Observation timestamps escape actual collection interval')
        bodies[role]=strict_json(github_content(entry['result'],entry['url']));entries[role]=entry;rss()
    need(set(expected_urls)<=set(bodies),'Missing actual base observation role')
    pr=bodies['pr'];need(type(pr['number']) is int and pr['number']==scope['pr'] and pr['state']=='open' and pr['merged'] is False and
        type(pr['draft']) is bool and pr['head']['sha']==head and pr['head']['ref']==scope['branch'] and
        pr['base']['sha']==base and pr['base']['ref']=='main','Current PR state/head/base/branch differs')
    for key in ('head','base'):need(type(pr[key]['repo']['id']) is int and pr[key]['repo']['id']==scope['repositoryId'] and pr[key]['repo']['full_name']==scope['repository'],'Current PR repository differs')
    for role,name,sha,expected_tree in [('mainBranch','main',base,scope['baseTree']),('headBranch',scope['branch'],head,tree)]:
        row=bodies[role];need(row['name']==name and row['commit']['sha']==sha and row['commit']['commit']['tree']['sha']==expected_tree,'Current branch commit/tree differs')
    for role,sha,expected_tree in [('headCommit',head,tree),('baseCommit',base,scope['baseTree']),('checkoutCommit',scope['testCheckout'],tree)]:
        row=bodies[role];need(row['sha']==sha and row['tree']['sha']==expected_tree,'Current Git commit/tree differs')
    need([p['sha'] for p in bodies['checkoutCommit']['parents']]==[base,head],'Test checkout parent order/identity differs')
    comparison=bodies['baseHeadCompare'];commits=comparison.get('commits')
    need(comparison.get('status')=='ahead' and type(comparison.get('behind_by')) is int and comparison['behind_by']==0 and
         type(comparison.get('ahead_by')) is int and type(comparison.get('total_commits')) is int and
         0<comparison['total_commits']==comparison['ahead_by']<=100 and type(commits) is list and
         len(commits)==comparison['total_commits'],'Base/head comparison is not a complete bounded ahead-only commit range')
    for key in ('base_commit','merge_base_commit'):
        need(comparison[key]['sha']==base and comparison[key]['commit']['tree']['sha']==scope['baseTree'],
             'Observed current base ancestry/merge base differs')
    need(all(type(row) is dict and type(row.get('sha')) is str and re.fullmatch('[0-9a-f]{40}',row['sha']) for row in commits) and
         len({row['sha'] for row in commits})==len(commits) and commits[-1]['sha']==head and
         commits[-1]['commit']['tree']['sha']==tree,'Observed comparison does not end at the exact current head/tree')
    changed={row['path']:(row['status'],row['gitBlob']) for row in plan['source']['changedFiles']}
    need(type(pr['changed_files']) is int and pr['changed_files']==len(changed),'Current PR changed-file count differs')
    file_rows=[]
    for page in range(1,max(1,(len(changed)+99)//100)+1):
        role='prFiles'+str(page);expected_urls[role]=api+f"/pulls/{scope['pr']}/files?per_page=100&page={page}"
        rows=bodies.get(role);need(type(rows) is list and len(rows)==min(100,max(0,len(changed)-100*(page-1))),'Current PR file pagination differs');file_rows.extend(rows)
    seen=set()
    for row in file_rows:
        path=row['filename'];need(path in changed and path not in seen and (row['status'],row['sha'])==changed[path],'Current PR source file/blob/status differs');seen.add(path)
    need(seen==set(changed),'Current PR file set incomplete')
    role_checks=[];actual_runs=[];attempt_starts={};all_job_ids=set();artifact_rows=[]
    for label,run_id in [('primary',scope['primaryRun']),('profile',scope['profileRun'])]:
        run=bodies[label+'Run'];attempt_number=check_run(run,run_id,plan)
        attempt_role=label+'Attempt';expected_urls[attempt_role]=api+f'/actions/runs/{run_id}/attempts/{attempt_number}'
        attempt=bodies.get(attempt_role);check_run(attempt,run_id,plan,attempt_number)
        for field in ('id','run_attempt','head_sha','head_branch','status','conclusion','path','check_suite_id','workflow_id','run_started_at'):
            need(run[field]==attempt[field],'Latest run and actual attempt disagree')
        start=timestamp(attempt['run_started_at']);need(start<=end,'Actual attempt starts after observation interval')
        attempt_starts[run_id]=start
        actual_runs.append({'id':run_id,'attempt':attempt_number,'status':'completed','conclusion':'success','head':head})
        jobs=_pages(bodies,expected_urls,label,'Jobs','jobs',api+f'/actions/runs/{run_id}/attempts/{attempt_number}/jobs')
        required=plan['ci']['requiredRoles'][label];publication=plan['ci']['publicationRole']
        expected_names=set(required)|({publication} if label=='primary' else set());observed=set()
        need(len(jobs)==len(expected_names),'Unexpected/missing actual attempt job count')
        for job in jobs:
            jid=job['id'];name=job['name']
            need(type(jid) is int and jid>0 and jid not in all_job_ids and name in expected_names and name not in observed,'Unexpected/duplicate actual job ID/name')
            need(type(job['run_id']) is int and job['run_id']==run_id and type(job['run_attempt']) is int and job['run_attempt']==attempt_number and
                 job['head_sha']==head and job['head_branch']==scope['branch'] and job['status']=='completed','Job exact attempt/source/completion differs')
            need(job['run_url']==api+f'/actions/runs/{run_id}' and job['url']==api+f'/actions/jobs/{jid}','Job canonical service URLs differ')
            need(job['conclusion']==('skipped' if name==publication else 'success'),'Required current role did not pass or publish state differs')
            observed.add(name);all_job_ids.add(jid)
            if name in required:role_checks.append({'id':jid,'name':name,'runId':run_id,'attempt':attempt_number,'status':'completed','conclusion':'success','head':head})
        need(observed==expected_names,'Missing actual current attempt roles')
        rows=_pages(bodies,expected_urls,label,'Artifacts','artifacts',api+f'/actions/runs/{run_id}/artifacts')
        artifact_rows.extend((run_id,row) for row in rows)
    need(set(bodies)==set(expected_urls),'Missing/extra actual pagination or attempt observation role')
    for role,url in expected_urls.items():need(entries[role]['url']==url,'Actual role canonical URL differs')
    required_count=sum(len(rows) for rows in plan['ci']['requiredRoles'].values())
    need(len(role_checks)==required_count and len({row['id'] for row in role_checks})==required_count,'Required current role identity derivation differs')
    catalogue_keys={'schemaVersion','kind','cohortManifest','repository','head','officialArtifacts','selectedArtifacts','logicalFamilies','runs'}
    need(type(catalogue) is dict and 'cohortManifest' in catalogue,'Catalogue lacks its pinned static intent')
    require_intent_reference(catalogue['cohortManifest'])
    need(type(catalogue) is dict and catalogue_keys<=set(catalogue) and set(catalogue)<=catalogue_keys|{'initialObservations'} and
         type(catalogue['schemaVersion']) is int and catalogue['schemaVersion']==1 and catalogue['kind']=='GDAL-cohort-official-artifact-catalogue' and
         catalogue['cohortManifest']==intent_ref and catalogue['repository']==scope['repository'] and catalogue['head']==head,'Pinned catalogue schema/source differs')
    catalogue_runs=catalogue['runs'];need(type(catalogue_runs) is list and len(catalogue_runs)==len(actual_runs),'Catalogue run inventory differs')
    for row in catalogue_runs:
        need(type(row) is dict and set(row)=={'id','attempt','status','conclusion','head'} and type(row['id']) is int and
             type(row['attempt']) is int and row['attempt']>0,'Catalogue run/attempt shape differs')
    need(sorted(catalogue_runs,key=lambda row:row['id'])==sorted(actual_runs,key=lambda row:row['id']),'Catalogue current actual run/attempt/source differs')
    if 'initialObservations' in catalogue:
        validate_reference(catalogue['initialObservations'],MAX_INPUT)
        ref=catalogue['initialObservations'];need(type(ref) is dict and set(ref)=={'path','bytes','sha256'} and type(ref['bytes']) is int and
             0<ref['bytes']<=MAX_INPUT and type(ref['sha256']) is str and re.fullmatch('[0-9a-f]{64}',ref['sha256']),'Catalogue initial observation reference differs')
    expected_all={};initial={}
    need(type(catalogue['officialArtifacts']) is list and 0<len(catalogue['officialArtifacts'])<=2*MAX_ROWS,'Catalogue official inventory exceeds bounds')
    for row in catalogue['officialArtifacts']:
        value=artifact(row,plan,attempt_starts,end);need(value['id'] not in expected_all,'Catalogue has duplicate official IDs');expected_all[value['id']]=value;initial[value['id']]=row
    actual_all={};names=set()
    for run_id,row in artifact_rows:
        value=artifact(row,plan,attempt_starts,end);key=(run_id,value['name'])
        need(value['runId']==run_id and value['id'] in expected_all and value['id'] not in actual_all and key not in names and
             value==expected_all[value['id']] and row['created_at']==initial[value['id']]['created_at'],'Current official artifact identity/partition/digest differs from pinned catalogue')
        actual_all[value['id']]=value;names.add(key)
    need(set(actual_all)==set(expected_all),'Complete current official artifact inventory differs')
    need(type(catalogue['selectedArtifacts']) is list,'Selected catalogue inventory is not a list')
    selected=[];selected_ids=set()
    for row in catalogue['selectedArtifacts']:
        value=artifact(row,plan,attempt_starts,end)
        need(value['id'] not in selected_ids and actual_all.get(value['id'])==value and value['size']<=32*MiB and
             row['created_at']==initial[value['id']]['created_at'],'Selected genuine ZIP identity differs')
        selected.append(value);selected_ids.add(value['id'])
    originals=[];used=[];original_ids=set();original_names=set();routes=Counter();run_attempts={row['id']:row['attempt'] for row in actual_runs}
    need(type(catalogue['logicalFamilies']) is list,'Logical family inventory is not a list')
    for family in catalogue['logicalFamilies']:
        need(type(family) is dict and set(family)=={'original','route','selected'},'Logical family schema differs')
        value=artifact(family['original'],plan,attempt_starts,end);name=value['name'];ids=family['selected']
        expected_run=scope['profileRun'] if name.startswith('profile-') else scope['primaryRun']
        need(name in plan['familyNames'] and name not in original_names and value['id'] not in original_ids and
             actual_all.get(value['id'])==value and value['runId']==expected_run and
             family['original']['created_at']==initial[value['id']]['created_at'],'Original logical family official identity differs')
        need(family['route'] in ('original-service-zip','audit-repack') and type(ids) is list and ids and
             all(type(aid) is int and aid in selected_ids for aid in ids) and len(set(ids))==len(ids),'Invalid family route/selected IDs')
        if family['route']=='original-service-zip':need(value['size']<=32*MiB and ids==[value['id']],'Direct route does not preserve the bounded original service ID')
        else:
            need(value['size']>32*MiB and value['id'] not in ids,'Repacked route improperly claims original service ZIP')
            label=re.sub('[^A-Za-z0-9_.-]','-',name)[:100] or 'source'
            prefix=f"audit-parts--{label}--{value['runId']}--{run_attempts[value['runId']]}--{value['id']}"
            selected_names=[actual_all[aid]['name'] for aid in ids]
            need(selected_names.count(prefix+'--index')==1,'Repacked family lacks one exact current-attempt index artifact')
            part_names=sorted(n for n in selected_names if n!=prefix+'--index')
            need(part_names and len(part_names)==len(ids)-1 and
                 set(part_names)=={prefix+'--part-'+str(index).zfill(3) for index in range(1,len(part_names)+1)} and
                 all(actual_all[aid]['runId']==value['runId'] for aid in ids),'Repacked family part names/attempt/source partition differs')
        routes[family['route']]+=1;used.extend(ids);original_ids.add(value['id']);original_names.add(name);originals.append(value)
    need(original_names==set(plan['familyNames']) and Counter(used)==Counter(selected_ids),'Current logical families do not exactly partition selected service ZIPs')
    rss()
    return {'schemaVersion':1,'kind':'GDAL-cohort-qualified-current-scope','cohortManifest':intent_ref,
        'observedStartedAtUtc':iso(begin),'observedCompletedAtUtc':iso(end),'qualifiedAtUtc':iso(now),'expiresAtUtc':iso(begin+dt.timedelta(seconds=age)),
        'maximumAgeSeconds':age,'repository':scope['repository'],'head':head,'tree':tree,'base':base,'baseTree':scope['baseTree'],
        'testCheckout':scope['testCheckout'],'checkoutParents':[base,head],'checkoutTree':tree,
        'pr':{'number':scope['pr'],'state':'open','draft':pr['draft'],'merged':False,'head':head,'base':base},
        'requiredRoleChecks':sorted(role_checks,key=lambda row:(row['runId'],row['id'])),'runs':sorted(actual_runs,key=lambda row:row['id']),
        'artifacts':sorted(selected,key=lambda row:row['id']),'originalArtifacts':sorted(originals,key=lambda row:row['id']),
        'officialArtifacts':sorted(actual_all.values(),key=lambda row:row['id']),'familyRoutes':dict(routes),
        'prFiles':[{'path':path,'status':changed[path][0],'sha':changed[path][1]} for path in sorted(changed)],
        'capabilityAccepted':False,'overallGoalComplete':False},entries


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    policy.add_arguments(parser)
    parser.add_argument('--input',required=True,type=Path);parser.add_argument('--input-sha256',required=True)
    parser.add_argument('--catalogue',required=True,type=Path);parser.add_argument('--catalogue-sha256',required=True)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();rss();read_pin(POLICY_REF,limit=MiB,sealed=True);policy.initialize_for_process()
    input_ref=reference(args.input,args.input_sha256);raw=read_pin(input_ref,sealed=True)
    read_pin(COLLECTOR_REF,limit=MiB,sealed=True)
    catalogue_ref=reference(args.catalogue,args.catalogue_sha256,limit=MAX_CATALOGUE);catalogue_raw=read_pin(catalogue_ref,limit=MAX_CATALOGUE,sealed=True)
    own_ref=reference(Path(__file__),limit=MiB);read_pin(own_ref,limit=MiB,sealed=True)
    record,catalogue=strict_json(raw),strict_json(catalogue_raw)
    scope,entries=qualify_record(record,catalogue,dt.datetime.now(dt.timezone.utc))
    parent,name=open_parent(args.output)
    try:os.mkdir(name,0o700,dir_fd=parent);directory=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);os.fsync(parent)
    finally:os.close(parent)
    charged=0
    try:
        def write(name,data):
            nonlocal charged
            need(len(data)<=MAX_INPUT and charged+len(data)<=MAX_OUTPUT,'Qualified metadata output bound exceeded')
            fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400,dir_fd=directory)
            with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
            ref={'path':str(args.output/name),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()};read_pin(ref,sealed=True);charged+=len(data);return ref
        observations=[]
        for role,entry in entries.items():
            operation=write(role+'.actual-operation.json',canonical(entry))
            body=write(role+'.actual-body.json',entry['result']['structuredContent']['content'].encode('utf-8'))
            observations.append({'role':role,'url':entry['url'],'before':entry['before'],'after':entry['after'],
                'actualObservationRecord':input_ref,'operation':operation,'body':body,
                'operationTransform':'Canonical JSON serialization of the exact parsed actual observation; original input record retained by whole SHA',
                'bodyTransform':'Exact UTF-8 encoding of actual result.structuredContent.content'})
        sources=[COLLECTOR_REF,own_ref,POLICY_REF]
        for ref in [input_ref,*sources]:read_pin(ref,sealed=True)
        read_pin(catalogue_ref,limit=MAX_CATALOGUE,sealed=True)
        now=dt.datetime.now(dt.timezone.utc);need(now<=clock(record['observedStartedAtUtc'])+dt.timedelta(seconds=scope['maximumAgeSeconds']),'Actual scope expired during output persistence')
        scope.update({'actualObservationRecord':input_ref,'catalogue':catalogue_ref,'sources':sources,'observations':observations,'processPeakRssBytes':rss()})
        scope_ref=write('qualified-current-scope.json',(json.dumps(scope,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode())
        summary={'schemaVersion':1,'kind':'GDAL-cohort-current-scope-qualification-closeout','cohortManifest':policy.current_ref(),
            'qualifiedScope':scope_ref,'input':input_ref,'sources':sources,'catalogue':catalogue_ref,'actualOperationCount':len(entries),
            'requiredRoles':len(scope['requiredRoleChecks']),'selectedServiceZIPs':len(scope['artifacts']),
            'selectedServiceZIPBytes':sum(row['size'] for row in scope['artifacts']),'originalLogicalFamilies':len(scope['originalArtifacts']),
            'expiresAtUtc':scope['expiresAtUtc'],'processPeakRssBytes':rss(),'capabilityAccepted':False,'overallGoalComplete':False}
        closeout=write('qualification-closeout.json',(json.dumps(summary,ensure_ascii=False,indent=2)+'\n').encode());os.fsync(directory)
    finally:os.close(directory)
    print(json.dumps({'qualifiedScope':scope_ref,'closeout':closeout,'expiresAtUtc':scope['expiresAtUtc']}))


if __name__=='__main__':main()


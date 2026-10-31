"""Derive a current catalogue from pinned normal collector metadata only.

No network calls or artifact bytes are read. The frozen current-scope qualifier
independently checks the entire observation record before a catalogue is emitted.
"""
from __future__ import annotations
import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import types

ROOT=Path(__file__).parent
MiB=1048576
SOURCE_LIMIT=MiB
SERVICE_ZIP_LIMIT=32*MiB
FROZEN_REFS = {'cohort_policy_v1': {'path': '/workspace/shared/gdal4k-c23-acceptance-tools-v1/cohort_policy_v1.py', 'bytes': 28680, 'sha256': 'ea15892360878b258cd6ab584c7b75be49a1d591c861a7a7fc599e8258346aa5'}, 'c23_scope_qualifier_v1': {'path': '/workspace/shared/gdal4k-c23-acceptance-tools-v1/c23_scope_qualifier_v1.py', 'bytes': 32530, 'sha256': 'a6811bd9c1a99fee92822f9acd642b560a9813b639a0cd250b7a3253ca978afb'}, 'collector': {'path': '/workspace/shared/gdal4k-c23-acceptance-tools-v1/c23_remote_scope_collector_v1.js', 'bytes': 10599, 'sha256': 'b81ece9d841703315cceb5b775733ff46dff8eaa2190c46ca74e7b260b9dee54'}}
_HELPERS=None


class CatalogueError(ValueError):pass


def need(value,message):
    if not value:raise CatalogueError(message)


def full11(value):
    return tuple(getattr(value,k) for k in ('st_dev','st_ino','st_mode','st_nlink','st_uid','st_gid','st_size','st_atime_ns','st_mtime_ns','st_ctime_ns','st_blocks'))


def open_parent(path):
    path=Path(path)
    need(path.is_absolute() and str(path)==str(Path(str(path))) and '..' not in path.parts,'Canonical absolute path required')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for name in path.parts[1:-1]:
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd,path.name
    except BaseException:os.close(fd);raise


def read_frozen(reference):
    """Bootstrap only the fixed small reviewed source closure, without atime."""
    need(type(reference) is dict and set(reference)=={'path','bytes','sha256'} and
         type(reference['bytes']) is int and 0<reference['bytes']<=SOURCE_LIMIT and
         type(reference['sha256']) is str and re.fullmatch('[0-9a-f]{64}',reference['sha256']),
         'Invalid reviewed source reference')
    parent,name=open_parent(reference['path'])
    try:fd=os.open(name,os.O_RDONLY|os.O_NOATIME|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    finally:os.close(parent)
    with os.fdopen(fd,'rb') as source:
        before=os.fstat(source.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_uid==os.getuid() and before.st_nlink==1 and
             stat.S_IMODE(before.st_mode)==0o400 and before.st_size==reference['bytes'],
             'Reviewed source must be exact owned0400/nlink1 regular file')
        raw=source.read(reference['bytes']+1)
        need(len(raw)==reference['bytes'] and hashlib.sha256(raw).hexdigest()==reference['sha256'],
             'Reviewed source whole length/SHA differs')
        need(full11(before)==full11(os.fstat(source.fileno()))==full11(os.lstat(reference['path'])),
             'Reviewed source full11 changed')
    return raw


def helpers():
    global _HELPERS
    if _HELPERS is None:
        raw={name:read_frozen(ref) for name,ref in FROZEN_REFS.items()}
        # Load exact reviewed bytes; do not read or create Python bytecode files.
        for name in ('cohort_policy_v1','c23_scope_qualifier_v1'):
            if name in sys.modules:
                need(getattr(sys.modules[name],'__file__',None)==FROZEN_REFS[name]['path'],
                     'Previously loaded helper path differs')
            else:
                module=types.ModuleType(name);module.__file__=FROZEN_REFS[name]['path']
                sys.modules[name]=module
                try:exec(compile(raw[name],module.__file__,'exec'),module.__dict__)
                except BaseException:del sys.modules[name];raise
        policy=sys.modules['cohort_policy_v1'];qualifier=sys.modules['c23_scope_qualifier_v1']
        need(qualifier.policy is policy and qualifier.COLLECTOR_REF==FROZEN_REFS['collector'] and
             qualifier.POLICY_REF==FROZEN_REFS['cohort_policy_v1'],'Frozen helper source closure differs')
        _HELPERS=policy,qualifier
    return _HELPERS


def _audit_prefix(name,run_id,attempt,artifact_id):
    label=re.sub('[^A-Za-z0-9_.-]','-',name)[:100] or 'source'
    return f'audit-parts--{label}--{run_id}--{attempt}--{artifact_id}'


def build_catalogue(record,now,initial_observations=None):
    """Derive metadata, then run the complete frozen qualifier before returning.

    The optional initial observation reference must describe the CLI's original
    sealed collector input. The pure API does not read that file independently.
    """
    policy,Q=helpers();plan=policy.current();scope=plan['scope'];Q.rss()
    need(type(record) is dict and type(record.get('observations')) is list,
         'Collector record/observations missing')
    Q.require_intent_reference(record.get('cohortManifest'))
    begin=Q.clock(record.get('observedStartedAtUtc'));end=Q.clock(record.get('observedCompletedAtUtc'))
    need(isinstance(now,dt.datetime) and now.tzinfo is not None and begin<=end<=now and
         (now-begin).total_seconds()<=plan['ci']['maximumAgeSeconds'],'Collector interval is future/stale/reversed')
    need(1<=len(record['observations'])<=9+5*Q.MAX_PAGES+2,'Collector operation count exceeds bound')
    bodies={};entries={}
    for entry in record['observations']:
        need(type(entry) is dict and set(entry)=={'role','url','before','after','result'} and
             type(entry['role']) is str and entry['role'] not in entries,'Invalid/duplicate collector operation')
        role=entry['role'];entries[role]=entry
        if re.fullmatch(r'(?:primary|profile)(?:Run|Attempt|Artifacts[1-9][0-9]*)',role):
            bodies[role]=Q.strict_json(Q.github_content(entry['result'],entry['url']))
    api='https://api.github.com/repos/'+scope['repository']
    expected_urls={};runs=[];starts={};official=[];by_id={};by_name={}
    for label,run_id in (('primary',scope['primaryRun']),('profile',scope['profileRun'])):
        run=bodies.get(label+'Run');attempt=Q.check_run(run,run_id,plan)
        actual=bodies.get(label+'Attempt');Q.check_run(actual,run_id,plan,attempt)
        starts[run_id]=Q.timestamp(actual['run_started_at'])
        need(starts[run_id]<=end,'Actual attempt starts after collector interval')
        runs.append({'id':run['id'],'attempt':attempt,'status':run['status'],
                     'conclusion':run['conclusion'],'head':run['head_sha']})
    for label,run_id in (('primary',scope['primaryRun']),('profile',scope['profileRun'])):
        rows=Q._pages(bodies,expected_urls,label,'Artifacts','artifacts',api+f'/actions/runs/{run_id}/artifacts')
        for row in rows:
            value=Q.artifact(row,plan,starts,end)
            need(value['runId']==run_id and value['id'] not in by_id and
                 (run_id,value['name']) not in by_name,'Duplicate/wrong-run official artifact')
            need(policy.integer(value['id']) and policy.integer(value['size']) and policy.string(value['name'],512),
                 'Official artifact metadata is not canonical typed GitHub metadata')
            by_id[value['id']]=row;by_name[(run_id,value['name'])]=row;official.append(row)
        Q.rss()
    for role,url in expected_urls.items():need(entries[role]['url']==url,'Artifact page canonical URL differs')
    artifact_roles={role for role in bodies if 'Artifacts' in role}
    need(artifact_roles==set(expected_urls),'Extra/missing artifact page observation')
    need(official,'Empty official artifact inventory')
    attempts={row['id']:row['attempt'] for row in runs}
    originals=[];prefixes={}
    for name in plan['familyNames']:
        run_id=scope['profileRun'] if name.startswith('profile-') else scope['primaryRun']
        matches=[row for row in official if row['name']==name]
        need(len(matches)==1 and matches[0]['workflow_run']['id']==run_id,
             'Missing/ambiguous/wrong-run original family: '+name)
        original=matches[0];originals.append(original)
        prefix=_audit_prefix(name,run_id,attempts[run_id],original['id'])
        need(prefix not in prefixes,'Ambiguous sanitized audit group prefix')
        prefixes[prefix]=(original,[])
    for row in official:
        name=row['name']
        if name.startswith('audit-parts--'):
            prefix,separator,suffix=name.rpartition('--')
            need(separator and prefix in prefixes and
                 (suffix=='index' or re.fullmatch(r'part-[0-9]{3,}',suffix)),
                 'Mismatched current audit prefix or malformed index/part name')
            original,group=prefixes[prefix]
            need(row['workflow_run']['id']==original['workflow_run']['id'],
                 'Audit group belongs to the wrong run')
            group.append(row)
    selected=[];families=[]
    for original in originals:
        prefix=_audit_prefix(original['name'],original['workflow_run']['id'],
                             attempts[original['workflow_run']['id']],original['id'])
        group=prefixes[prefix][1]
        if group:
            names={row['name'] for row in group};parts=sorted(name for name in names if name!=prefix+'--index')
            need(prefix+'--index' in names and parts and len(names)==len(group) and
                 set(parts)=={prefix+'--part-'+str(index).zfill(3) for index in range(1,len(parts)+1)},
                 'Audit group lacks exact one index and contiguous numbered parts')
        if original['size_in_bytes']>SERVICE_ZIP_LIMIT:
            need(group,'Oversized original has no complete current audit group')
            chosen=sorted(group,key=lambda row:(row['name']!=prefix+'--index',row['name']))
            route='audit-repack'
        else:
            # The frozen qualifier requires bounded originals to retain their
            # own service ZIP identity, even if a valid redundant group exists.
            chosen=[original];route='original-service-zip'
        for row in chosen:
            need(type(row['name']) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,511}',row['name']) and
                 type(row['size_in_bytes']) is int and 0<row['size_in_bytes']<=SERVICE_ZIP_LIMIT,
                 'Selected service ZIP has unsafe name or exceeds 32 MiB')
        selected.extend(chosen)
        families.append({'original':original,'route':route,'selected':[row['id'] for row in chosen]})
    need(len({row['id'] for row in selected})==len(selected),'Selected service ZIPs overlap families')
    catalogue={'schemaVersion':1,'kind':'GDAL-cohort-official-artifact-catalogue',
        'cohortManifest':policy.current_ref(),'repository':scope['repository'],'head':scope['head'],
        'officialArtifacts':sorted(official,key=lambda row:row['id']),
        'selectedArtifacts':sorted(selected,key=lambda row:row['id']),
        'logicalFamilies':families,'runs':sorted(runs,key=lambda row:row['id'])}
    if initial_observations is not None:
        Q.validate_reference(initial_observations,Q.MAX_INPUT)
        need(initial_observations['bytes']>0,'Initial collector input reference is empty')
        catalogue['initialObservations']=dict(initial_observations)
    need(len(Q.canonical(catalogue))<=Q.MAX_CATALOGUE,'Derived catalogue exceeds reviewed 2 MiB bound')
    Q.qualify_record(record,catalogue,now)
    Q.rss();return catalogue


def main():
    policy,Q=helpers()
    parser=argparse.ArgumentParser(description=__doc__);policy.add_arguments(parser)
    parser.add_argument('--input',required=True,type=Path);parser.add_argument('--input-sha256',required=True)
    parser.add_argument('--source-sha256',required=True);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();Q.rss();policy.initialize_for_process()
    own_ref=Q.reference(Path(__file__),args.source_sha256,limit=SOURCE_LIMIT)
    Q.read_pin(own_ref,limit=SOURCE_LIMIT,sealed=True)
    input_ref=Q.reference(args.input,args.input_sha256,limit=Q.MAX_INPUT)
    raw=Q.read_pin(input_ref,limit=Q.MAX_INPUT,sealed=True);record=Q.strict_json(raw)
    catalogue=build_catalogue(record,dt.datetime.now(dt.timezone.utc),input_ref)
    data=Q.canonical(catalogue)
    # Recheck exact inputs and current freshness immediately before O_EXCL output.
    Q.read_pin(input_ref,limit=Q.MAX_INPUT,sealed=True)
    Q.read_pin(own_ref,limit=SOURCE_LIMIT,sealed=True)
    for reference in FROZEN_REFS.values():read_frozen(reference)
    Q.qualify_record(record,catalogue,dt.datetime.now(dt.timezone.utc))
    parent,name=open_parent(args.output)
    try:
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        with os.fdopen(fd,'wb') as target:
            target.write(data);target.flush();os.fsync(target.fileno());os.fchmod(target.fileno(),0o400);os.fsync(target.fileno())
        os.fsync(parent)
    finally:os.close(parent)
    output={'path':str(args.output),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    Q.read_pin(output,limit=Q.MAX_CATALOGUE,sealed=True)
    print(json.dumps({'catalogue':output,'cohortManifest':policy.current_ref(),'initialObservations':input_ref,
        'runs':catalogue['runs'],'officialArtifacts':len(catalogue['officialArtifacts']),
        'selectedArtifacts':len(catalogue['selectedArtifacts']),'logicalFamilies':len(catalogue['logicalFamilies']),
        'familyRoutes':dict(Counter(row['route'] for row in catalogue['logicalFamilies'])),
        'sources':[own_ref,*FROZEN_REFS.values()],'processPeakRssBytes':Q.rss(),
        'capabilityAccepted':False,'overallGoalComplete':False}))


if __name__=='__main__':main()

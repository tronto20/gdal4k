"""Root-controlled SAME-job audit executor. No predicate implementation lives here."""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import resource
import stat
import subprocess
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from protocol import (M,MAX_METADATA,absolute,canonical,create,digest,full11,integer,need,
                      parents,parse,pin,read_bytes,read_pin,reference,relative,same,seal,site,toolkit,
                      runtime_resources,admit_runtime)
from export_proof_stream import export

GATES=('sourceBindings','maven','mavenAliasIndependent','defaultArm64','combinedArm64',
       'windowsReadiness','platformScopes','aggregateCurrent','finalAcceptance')
STAGES=('prepare','headerPrepare','header','body',*GATES)
IMAGE='docker.io/library/python@sha256:034c3e9d991f148d14c32ff8350286b91420baca7757d37819c7a9cc771317a0'
def now():return dt.datetime.now(dt.timezone.utc).isoformat()
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('Git control API redirect forbidden')

class GitAPI:
    def __init__(self,repo,token):
        need(type(repo)is str and len(repo.split('/'))==2 and token,'Read-only job token/repository required')
        self.base='https://api.github.com/repos/'+repo;self.token=token
        self.opener=urllib.request.build_opener(NoRedirect())
    def get(self,path,maximum=MAX_METADATA):
        need(path.startswith('/')and not path.startswith('//'),'Relative GitHub API route required')
        url=self.base+path;before=now()
        request=urllib.request.Request(url,headers={'Authorization':'Bearer '+self.token,
            'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'})
        with self.opener.open(request,timeout=30)as response:
            need(response.status==200 and response.geturl()==url,'Git API status/origin differs')
            raw=response.read(maximum+1);need(len(raw)<=maximum,'Git response bound')
        return raw,{'method':'GET','url':url,'status':200,'bodySha256':hashlib.sha256(raw).hexdigest(),
                    'body':parse(raw),'beforeUtc':before,'afterUtc':now()}
    def blob(self,oid,expected_bytes):
        digest(oid,40);raw,_=self.get('/git/blobs/'+oid,12*M);value=parse(raw)
        need(value.get('sha')==oid and value.get('encoding')=='base64'and value.get('size')==expected_bytes,'Git blob metadata differs')
        body=base64.b64decode(value['content'],validate=False)
        need(len(body)==expected_bytes and hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()==oid,'Git blob bytes differ')
        return body

def source_rows(state):return state['sourcePins']+state['auxiliary']
def check_sources(state):
    result=[]
    for ref in source_rows(state):
        raw,stats=read_bytes(ref['path'],2*M)
        need(len(raw)==ref['bytes']and hashlib.sha256(raw).hexdigest()==ref['sha256'],'Frozen kit source bytes differ')
        result.append({'reference':ref,'full11':stats})
    return result

def init(args):
    kitdir=absolute(str(Path(args.kit).resolve()));digest(args.kit_commit,40);digest(args.manifest_sha256)
    git=subprocess.run(['git','-C',str(kitdir),'rev-parse','HEAD'],capture_output=True,check=True)
    need(git.stdout.decode().strip()==args.kit_commit,'Immutable kit checkout differs')
    raw,_=read_bytes(kitdir/'kit.json',2*M,sealed=False)
    need(hashlib.sha256(raw).hexdigest()==args.manifest_sha256,'Kit manifest differs');manifest=parse(raw)
    need(set(manifest)=={'schemaVersion','kind','codeRoot','sourcePins','files','hostFiles','actors','loader','outerRunner','materializer'}and
         type(manifest['schemaVersion'])is int and manifest['schemaVersion']==1 and manifest['kind']=='GDAL-remote-audit-kit','Kit schema differs')
    root=absolute(args.state_root);need(not root.exists()and root.is_relative_to('/workspace/shared/gdal4k-remote-audit'),'Fresh remote state required')
    code=absolute(manifest['codeRoot']);need(str(code)=='/workspace/shared/gdal4k-c23-acceptance-tools-v1','Exact source20 namespace required')
    need(type(manifest['sourcePins'])is list and len(manifest['sourcePins'])==20 and len({r['path']for r in manifest['sourcePins']})==20,'Exact unique source20 required')
    for row in manifest['files']:
        need(set(row)=={'gitPath','remotePath','bytes','sha256'},'Kit file fields differ');relative(row['gitPath'])
        target=absolute(row['remotePath']);need(target.is_relative_to(code),'Kit destination escapes code root')
        raw,_=read_bytes(kitdir/row['gitPath'],2*M,sealed=False)
        need(type(row['bytes'])is int and len(raw)==row['bytes']and hashlib.sha256(raw).hexdigest()==row['sha256'],'Kit file hash differs')
        create(target,raw)
    pfd=parents(root,True)
    try:os.mkdir(root.name,0o700,dir_fd=pfd);os.fsync(pfd)
    finally:os.close(pfd)
    repo=os.environ['GITHUB_REPOSITORY'];head=digest(os.environ['GDAL_AUDIT_HEAD'],40)
    run=integer(int(os.environ['GITHUB_RUN_ID']),1);attempt=integer(int(os.environ['GITHUB_RUN_ATTEMPT']),1)
    jobkey=os.environ['GITHUB_JOB'];api=GitAPI(repo,os.environ['GITHUB_TOKEN'])
    raw,operation=api.get('/actions/runs/'+str(run)+'/attempts/'+str(attempt)+'/jobs?per_page=100')
    body=operation['body'];jobs=[j for j in body.get('jobs',[])if j.get('name')==jobkey and j.get('run_id')==run and j.get('head_sha')==head]
    need(len(jobs)==1 and type(jobs[0].get('id'))is int and
         type(jobs[0].get('run_attempt'))is int and jobs[0]['run_attempt']==attempt and
         jobs[0].get('status')=='in_progress','Actual unique audit job/attempt not observed')
    execution={'repository':repo,'head':head,'runId':run,'attempt':attempt,'jobId':jobs[0]['id'],'uid':os.getuid()};site(execution)
    toolkit_id={'commit':args.kit_commit,'manifestSha256':args.manifest_sha256};toolkit(toolkit_id)
    rawref=create(root/'job-list-raw.json',raw);operation['rawBody']=rawref
    opref=seal(root/'job-observation.json',operation)
    context=seal(root/'worker-context.json',{'schemaVersion':1,'kind':'GDAL-remote-audit-worker-context','site':execution,
        'jobKey':jobkey,'workflowSHA':digest(os.environ['GITHUB_SHA'],40),'jobObservation':opref})
    refs={r['remotePath']:{'path':r['remotePath'],'bytes':r['bytes'],'sha256':r['sha256']}for r in manifest['files']}
    for ref in manifest['sourcePins']:need(same(ref,refs.get(ref['path'])),'Source20 differs from staged kit')
    named={Path(r['path']).name:r for r in manifest['sourcePins']}
    expected_actors={'prepare':manifest['materializer'],'headerPrepare':named['c23_header_census_v1.py'],
        'header':named['c23_header_census_v1.py'],'body':named['c23_serial_family_driver_v1.py'],
        **{name:named['c23_semantic_gates_v1.py']for name in GATES}}
    need(same(manifest['actors'],expected_actors),'Only the exact frozen stage registry is allowed')
    state={'site':execution,'toolkit':toolkit_id,'sourcePins':manifest['sourcePins'],
        'auxiliary':[r for r in refs.values()if r['path']not in {s['path']for s in manifest['sourcePins']}],
        'actors':manifest['actors'],'loader':manifest['loader'],'outerRunner':manifest['outerRunner'],
        'materializer':manifest['materializer'],'context':context,'stateRoot':str(root),
        'codeRoot':str(code),'sourceCheckout':str(absolute(str(Path(args.source_checkout).resolve()))),
        'controlRef':'codex/gdal-audit-control-'+head+'-'+str(run)+'-'+str(attempt),
        'realInterpreter':str(Path(sys.executable).resolve())}
    check_sources(state);observed=runtime_resources();admit_runtime(observed,memory_extra=648*M)
    need(min(g['limitBytes']for g in observed['cgroups']if g['limitBytes']is not None)==8*1024**3,'Declared owned8GiB cap was not observed')
    seal(root/'state.json',state)
    gitpath=shutil.which('git');need(gitpath,'Pinned full image must supply real Git')
    version=subprocess.run([gitpath,'--version'],capture_output=True,check=True)
    need(len(version.stdout)<=1024 and version.stdout.startswith(b'git version '),'Git runtime observation failed')
    seal(root/'runtime.json',{'site':execution,'declaredImage':IMAGE,'containerId':os.environ.get('GDAL_AUDIT_CONTAINER_ID'),
        'imageIdObservation':'Container has no Docker socket; Root must join Actions container-init log to the pinned image.',
        'actualImageId':os.environ.get('GDAL_AUDIT_IMAGE_ID'),'gitPath':str(Path(gitpath).resolve()),
        'gitVersion':version.stdout.decode().strip(),'gitFull11':full11(os.stat(gitpath)),
        'pythonVersion':sys.version,'resources':runtime_resources(),
        'interpreter':state['realInterpreter'],'interpreterFull11':full11(os.stat(state['realInterpreter']))})
    print(json.dumps({'state':pin(root/'state.json'),'context':context,'controlRef':state['controlRef']}))

def get_command(api,state,sequence,previous,wait_seconds):
    deadline=time.monotonic()+wait_seconds
    while True:
        try:raw,_=api.get('/git/ref/heads/'+urllib.parse.quote(state['controlRef'],safe='/'))
        except urllib.error.HTTPError as error:
            if error.code!=404:raise
            raw=None
        if raw is not None:
            tip=parse(raw)['object']['sha'];digest(tip,40)
            if previous is None or tip!=previous['inputCommit']:
                raw,_=api.get('/contents/command.json?ref='+tip,2*M);entry=parse(raw)
                need(entry.get('encoding')=='base64','Root command encoding differs')
                data=base64.b64decode(entry['content']);need(len(data)<=M,'Root command1MiB limit')
                need(entry.get('size')==len(data)and type(entry.get('size'))is int and
                     hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()==entry.get('sha'),
                     'Root command wire bytes differ from the committed blob')
                command=parse(data)
                need(type(command)is dict and set(command)=={'schemaVersion','kind','rootApprovedActualInputs','site','toolkit',
                    'sequence','stage','previousControlCommit','previousReceiptSha256','scope','files','actor','export'},'Root command schema differs')
                need(type(command['schemaVersion'])is int and command['schemaVersion']==1 and
                    command['kind']=='GDAL-remote-audit-root-command'and command['rootApprovedActualInputs']is True and
                    same(command['site'],state['site'])and same(command['toolkit'],state['toolkit'])and
                    type(command['sequence'])is int and command['sequence']==sequence and command['stage']==STAGES[sequence]and
                    command['previousControlCommit']==(None if previous is None else previous['inputCommit'])and
                    command['previousReceiptSha256']==(None if previous is None else previous['_receipt']['sha256']),
                    'Root CAS/sequence/site/previous-result authority differs')
                gitraw,_=api.get('/git/commits/'+tip);gitcommit=parse(gitraw)
                need([p['sha']for p in gitcommit['parents']]==([state['site']['head']]if previous is None else[previous['inputCommit']]),
                     'Root control Git parent/CAS chain differs')
                tree_raw,_=api.get('/git/trees/'+gitcommit['tree']['sha']+'?recursive=1');tree=parse(tree_raw)
                need(tree.get('truncated')is False and type(tree.get('tree'))is list,'Complete Root input Git tree required')
                blobs={row['sha']for row in tree['tree']if row.get('type')=='blob'and row.get('mode')=='100644'}
                need(entry.get('sha')in blobs and all(row.get('gitBlob')in blobs for row in command['files']),
                     'Root command/input blobs are not members of the immutable input commit')
                reference(command['scope']);return tip,command,data
        need(time.monotonic()<deadline,'Root control wait deadline exceeded')
        time.sleep(20)

MAX_NEW_INPUT_BYTES=64*M

def input_destination(target):
    """Observe the existing destination device without creating any directory."""
    ancestor=target.parent;missing=[]
    while not os.path.lexists(ancestor):missing.append(str(ancestor));ancestor=ancestor.parent
    fd=parents(ancestor/'observation-only')
    try:s=os.fstat(fd);v=os.fstatvfs(fd)
    finally:os.close(fd)
    return {'device':s.st_dev,'fragment':max(4096,v.f_frsize),'missing':missing}

def direct_namespace_charge(parent):
    if not os.path.lexists(parent):return 4096
    fd=parents(parent/'observation-only')
    try:
        charge=os.fstat(fd).st_blocks*512
        for name in os.listdir(fd):charge+=os.stat(name,dir_fd=fd,follow_symlinks=False).st_blocks*512
        return charge
    finally:os.close(fd)

def admit_inputs(newrows,remaining):
    # The observed filesystems retain their ordinary32MiB/2GiB floors. New
    # inputs charge each real destination device, including aliases, in full.
    sample=runtime_resources();total=sum(remaining.values())
    if not remaining:admit_runtime(sample,memory_extra=424*M);return
    for device,charge in remaining.items():
        representative=next(path for path,info in newrows if info['device']==device)
        admit_runtime(sample,memory_extra=424*M+total,output=representative,output_reservation=charge)

def install_inputs(api,state,files):
    need(type(files)is list and len(files)<=500,'Root input inventory bound')
    admit_runtime(runtime_resources(),memory_extra=424*M)
    seen=set();parents_to_check=set();protected={r['path']for r in source_rows(state)}
    pending=[];newrows=[];remaining={};namespace_add={};newbytes=0;missingdirs=set()
    for row in files:
        need(type(row)is dict and set(row)=={'remotePath','gitBlob','bytes','sha256','mode'},'Root input file fields differ')
        target=absolute(row['remotePath']);need(str(target)not in seen,'Duplicate input path');seen.add(str(target))
        need((target.is_relative_to('/workspace/shared')or target.is_relative_to('/dev/shm'))and
             not target.is_relative_to(state['stateRoot'])and not target.is_relative_to(state['codeRoot'])and
             str(target)not in protected,'Input overwrites protected state/code namespace')
        integer(row['bytes'],0,MAX_METADATA);digest(row['sha256']);digest(row['gitBlob'],40)
        need(type(row['mode'])is int and row['mode']in (0o400,0o500)and
             (row['mode']==0o400 or target.name=='python3'),'Input mode differs')
        need(target.suffix in('.json','.jsonl','.txt','.log','.bin')or row['mode']==0o500,
             'Root input must be metadata, not an import-shadow source dependency')
        if os.path.lexists(target):
            existing,_=read_bytes(target,MAX_METADATA,sealed=row['mode']==0o400)
            need(len(existing)==row['bytes']and hashlib.sha256(existing).hexdigest()==row['sha256']and
                 stat.S_IMODE(os.lstat(target).st_mode)==row['mode'],'Existing immutable input differs')
        else:
            info=input_destination(target);block=info['fragment']
            charge=((row['bytes']+block-1)//block)*block+4096
            for directory in info['missing']:
                if directory not in missingdirs:missingdirs.add(directory);charge+=4096
            newbytes+=row['bytes'];need(newbytes<=MAX_NEW_INPUT_BYTES,'New Root input batch64MiB bound')
            remaining[info['device']]=remaining.get(info['device'],0)+charge
            namespace_add[target.parent]=namespace_add.get(target.parent,0)+((row['bytes']+block-1)//block)*block+4096
            pending.append((target,row,info,charge));newrows.append((target,info))
        parents_to_check.add(target.parent)
    for parent in parents_to_check:
        need(direct_namespace_charge(parent)+namespace_add.get(parent,0)<=8*M,'Prospective Root input namespace8MiB bound')
    admit_inputs(newrows,remaining)
    for target,row,info,charge in pending:
        need(input_destination(target)['device']==info['device'],'Input destination device changed')
        admit_inputs(newrows,remaining)
        body=api.blob(row['gitBlob'],row['bytes']);need(hashlib.sha256(body).hexdigest()==row['sha256'],'Root input SHA differs')
        create(target,body,row['mode']);remaining[info['device']]-=charge
        need(direct_namespace_charge(target.parent)<=8*M,'Actual Root input namespace8MiB bound')
        admit_inputs(newrows,remaining)
    for parent in parents_to_check:
        need(direct_namespace_charge(parent)<=8*M,'Root input direct metadata namespace8MiB bound')
    return [row['remotePath']for row in files]

BOOTSTRAP="""import os,sys,hashlib,linecache,traceback,json,stat
r=json.loads(sys.argv[1]);fd=os.open(r['path'],os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
with os.fdopen(fd,'rb')as f:
 before=os.fstat(f.fileno());b=f.read(2097153);after=os.fstat(f.fileno())
keys=('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_atime_ns','st_mtime_ns','st_ctime_ns','st_blocks')
assert tuple(getattr(before,k)for k in keys)==tuple(getattr(after,k)for k in keys)==tuple(getattr(os.lstat(r['path']),k)for k in keys)
assert stat.S_ISREG(before.st_mode)and before.st_uid==os.getuid()and before.st_nlink==1 and stat.S_IMODE(before.st_mode)==0o400
assert len(b)==r['bytes'] and hashlib.sha256(b).hexdigest()==r['sha256']
linecache.cache[r['path']]=(len(b),None,b.decode().splitlines(True),r['path'])
sys.excepthook=lambda t,v,tb:traceback.print_exception(t,v,tb)
sys.argv=[r['path'],*sys.argv[2:]]
exec(compile(b,r['path'],'exec'),{'__name__':'__main__','__file__':r['path']})
"""
def loader_code(state,policy_ref):
    """The only permitted dynamic wrapper is this fixed NoTime loader call."""
    return """import os,sys,hashlib,linecache,types
r=%r
fd=os.open(r['path'],os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
with os.fdopen(fd,'rb')as f:
 before=os.fstat(f.fileno());b=f.read(2097153);after=os.fstat(f.fileno())
keys=('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_atime_ns','st_mtime_ns','st_ctime_ns','st_blocks')
assert tuple(getattr(before,k)for k in keys)==tuple(getattr(after,k)for k in keys)==tuple(getattr(os.lstat(r['path']),k)for k in keys)
assert before.st_uid==os.getuid()and before.st_nlink==1 and before.st_mode&0o7777==0o400
assert len(b)==r['bytes']and hashlib.sha256(b).hexdigest()==r['sha256']
linecache.cache[r['path']]=(len(b),None,b.decode().splitlines(True),r['path'])
n=types.ModuleType('verified_remote_notime');n.__file__=r['path'];exec(compile(b,r['path'],'exec'),n.__dict__)
n.run(%r,list(sys.argv[1:]))
"""%(state['loader'],policy_ref)

def wrapper_bytes(state,policy_ref):
    code=loader_code(state,policy_ref)
    return ('#!'+state['realInterpreter']+'\nimport os,sys\na=sys.argv[1:]\n'
        'assert a and a[0]=="-B"\na=a[1:]\n'
        'os.execv(%r,[%r,"-B","-c",%r,*a])\n'%
        (state['realInterpreter'],state['realInterpreter'],code)).encode()

def cached_run(state,source,args,capture):
    reference(source,2*M);read_pin(source,False,2*M)
    need(not capture.exists(),'Fresh outer bootstrap capture required');os.mkdir(capture,0o700)
    # Frozen outer runner writes only a tiny final ref. Oversize is an incident,
    # never an excuse to kill/pretend an actor completed successfully.
    child=subprocess.Popen([state['realInterpreter'],'-B','-c',BOOTSTRAP,canonical(source).decode(),*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,close_fds=True)
    complete=[];errors=[]
    def drain(stream,name):
        count=0;whole=hashlib.sha256();output=None
        try:
            fd=os.open(capture/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            output=os.fdopen(fd,'wb')
        except OSError as error:errors.append({'stream':name,'errno':error.errno})
        try:
            while True:
                data=stream.read(65536)
                if not data:break
                whole.update(data);before=count;count+=len(data)
                if output is not None and before<2*M:
                    try:output.write(data[:2*M-before])
                    except OSError as error:errors.append({'stream':name,'errno':error.errno})
            if output is not None:
                try:output.flush();os.fsync(output.fileno());os.fchmod(output.fileno(),0o400)
                except OSError as error:errors.append({'stream':name,'errno':error.errno})
        finally:
            if output is not None:
                try:output.close()
                except OSError as error:errors.append({'stream':name,'errno':error.errno})
            stream.close()
        complete.append({'stream':name,'bytes':count,'wholeSha256':whole.hexdigest(),'complete':count<=2*M and not any(e['stream']==name for e in errors)})
    threads=[threading.Thread(target=drain,args=(child.stdout,'stdout.bin')),threading.Thread(target=drain,args=(child.stderr,'stderr.bin'))]
    for thread in threads:thread.start()
    code=child.wait()
    for thread in threads:thread.join()
    seal(capture/'bootstrap-result.json',{'actualPid':child.pid,'exitCode':code,'source':source,'streams':complete,'captureErrors':errors})
    need(len(complete)==2 and all(x['complete']for x in complete),'Bootstrap capture limit incident; full stream drained')
    return code

def prepare(state,actor):
    activation=read_pin(actor['preparation']);need(activation['rootApprovedPrepare']is True,'Root preparation not approved')
    intent_ref=activation['cohortManifest'];intent=read_pin(intent_ref);project=absolute(intent['source']['root'])
    need(project.parent==Path('/workspace')and project.name!='shared'and not project.exists(),'Fresh exact source checkout root required')
    subprocess.run(['git','clone','--no-hardlinks','--no-checkout',state['sourceCheckout'],str(project)],check=True,stdout=subprocess.DEVNULL)
    subprocess.run(['git','-C',str(project),'checkout','--detach',state['site']['head']],check=True,stdout=subprocess.DEVNULL)
    observed=subprocess.run(['git','-C',str(project),'rev-parse','HEAD','HEAD^{tree}',intent['scope']['base']+'^{tree}'],
        capture_output=True,check=True).stdout.decode().splitlines()
    need(observed==[intent['scope']['head'],intent['scope']['tree'],intent['scope']['baseTree']],
         'Observed checkout HEAD/tree/baseTree differs from Root intent')
    args=['--intent',intent_ref['path'],intent_ref['sha256'],'--catalogue',activation['sourceCatalogue']['path'],activation['sourceCatalogue']['sha256'],
        '--activation',actor['preparation']['path'],actor['preparation']['sha256'],'--worker-context',state['context']['path'],state['context']['sha256'],
        '--output-root',activation['outputRoot'],'--raw-root',activation['rawRoot']]
    need(actor['argv']==[]and same(actor['source'],state['materializer']),'Prepare is a fixed materializer action, not a shell')
    for path in (activation['outputRoot'],activation['rawRoot']):
        fd=parents(path,True);os.close(fd)
    code=cached_run(state,state['materializer'],args,Path(state['stateRoot'])/'prepare-capture')
    need(code==0,'Materializer failed; retained new partial facts')
    material=pin(Path(activation['outputRoot'])/'prepared.json');value=read_pin(material)
    need(value.get('materializationMap'),'Actual materialization map absent')
    checkout={'head':observed[0],'tree':observed[1],'base':intent['scope']['base'],'baseTree':observed[2]}
    prepared=seal(Path(state['stateRoot'])/'worker-prepared.json',{'schemaVersion':1,'kind':'GDAL-remote-audit-worker-prepared',
        'site':state['site'],'toolkit':state['toolkit'],'sourcePins':state['sourcePins'],
        'sourceFull11':check_sources(state)[:20],'materializerPrepared':material,'checkout':checkout,
        'rawRoot':activation['rawRoot'],'rootSeedCommit':state['_inputCommit']})
    return prepared,['python3','-B',state['materializer']['path'],*args],None

def validate_actor(state,stage,actor):
    need(type(actor)is dict and set(actor)=={'source','argv','plan','registration','sourcePolicy','outerConfig','preparation'},'Actor descriptor fields differ')
    need(same(actor['source'],state['actors'][stage]),'Actor is not the frozen allowlisted stage source')
    if stage=='prepare':return
    argv=actor['argv'];need(type(argv)is list and all(type(x)is str for x in argv)and
        argv[:3]==['python3','-B',actor['source']['path']]and len(argv)<=32,'Exact no-shell Python actor argv required')
    for flag,ref in (('--plan',actor['plan']),('--registration',actor['registration'])):
        if ref is None:need(flag not in argv,'Unexpected actor input flag');continue
        reference(ref);need(argv.count(flag)==1 and argv[argv.index(flag)+1]==ref['path'],'Actor input path differs')
        sha=flag+'-sha256';need(argv.count(sha)==1 and argv[argv.index(sha)+1]==ref['sha256'],'Actor input SHA differs')
    config=read_pin(actor['outerConfig']);policy=read_pin(actor['sourcePolicy'])
    need(same(config['requestedArgv'],argv)and same(config['policy'],actor['sourcePolicy'])and
        config['realInterpreter']==state['realInterpreter'],'Root outer argv/policy/interpreter differs')
    pins={r['reference']['path']:r['reference']for r in policy['sources']}
    need(all(same(pins.get(ref['path']),ref)for ref in state['sourcePins']),'Root NoTime policy omits source20')
    need(policy['sourceRoot']==state['codeRoot']and policy['realInterpreter']==state['realInterpreter']and
         same(policy['interpreterFull11'],full11(os.stat(state['realInterpreter']))),'Remote interpreter/source policy differs')
    wrapper_ref=reference(config['wrapper'],2*M);wrapper,_=read_bytes(wrapper_ref['path'],2*M,sealed=False)
    need(stat.S_IMODE(os.lstat(wrapper_ref['path']).st_mode)==0o500 and len(wrapper)==wrapper_ref['bytes']and
         hashlib.sha256(wrapper).hexdigest()==wrapper_ref['sha256'],'Pinned executable wrapper bytes/mode differ')
    need(same(config['loader'],state['loader'])and wrapper==wrapper_bytes(state,actor['sourcePolicy']),
         'Only the fixed verified NoTime wrapper is allowed; no dynamic shell/program')
    for field in ('loader','policy','envelopeApproval','actorInputApproval'):read_pin(config[field],False,2*M)
    outer_output=absolute(config['outputRoot'])
    need(config['cwd']=='/workspace'and(outer_output.is_relative_to('/workspace/shared')or outer_output.is_relative_to('/dev/shm'))and
         not outer_output.is_relative_to(state['stateRoot'])and not outer_output.is_relative_to(state['codeRoot']),'Outer cwd/output escapes the reviewed metadata layout')
    allowed_flags={
        'headerPrepare':{'--cohort-manifest','--cohort-manifest-sha256','--catalogue-ref','--materialization-map-ref','--source-approval-ref','--scratch-root','--output'},
        'header':{'--cohort-manifest','--cohort-manifest-sha256','--plan','--plan-sha256','--output'},
        'body':{'--cohort-manifest','--cohort-manifest-sha256','--config','--config-sha256','--output'}}
    flags=[value for value in argv[3:]if value.startswith('--')]
    expected=allowed_flags.get(stage,{'--cohort-manifest','--cohort-manifest-sha256','--plan','--plan-sha256','--registration','--registration-sha256','--output'})
    need(set(flags)==expected and len(flags)==len(expected),'Exact allowlisted actor flag inventory differs')
    if stage=='headerPrepare':need('prepare-plan'in argv and 'run'not in argv,'Header prepare subcommand differs')
    if stage=='header':need('run'in argv and 'prepare-plan'not in argv,'Header run subcommand differs')
    output=absolute(argv[argv.index('--output')+1])
    need(output.is_relative_to('/workspace/shared')or output.is_relative_to('/dev/shm'),'Actor output escapes metadata namespaces')
    need(not output.is_relative_to(state['stateRoot'])and not output.is_relative_to(state['codeRoot'])and output.name not in{Path(r['path']).name for r in source_rows(state)},'Actor output overlaps worker state/source')
    if stage in GATES:need(read_pin(actor['plan']).get('gate')==stage,'Semantic stage differs from its exact plan')

def validate_scope(state,command,actor,stage):
    ref=reference(command['scope'])
    need(any(same(ref,{'path':r['remotePath'],'bytes':r['bytes'],'sha256':r['sha256']})for r in command['files']),
         'Current scope must be an exact Root input member of this command')
    scoped=read_pin(ref)
    start=dt.datetime.fromisoformat(scoped['observedStartedAtUtc']);expiry=dt.datetime.fromisoformat(scoped['expiresAtUtc'])
    need(scoped['kind']=='GDAL-cohort-qualified-current-scope'and type(scoped['schemaVersion'])is int and scoped['schemaVersion']==1 and
         scoped['head']==state['site']['head']and start.tzinfo is not None and expiry.tzinfo is not None and
         0<(expiry-start).total_seconds()<=600 and start<=dt.datetime.now(dt.timezone.utc)<expiry,'Fresh genuine Root scope required')
    if stage in GATES:need(same(read_pin(actor['plan'])['scope'],ref),'Actor and Root command scopes differ')

def step(args):
    root=absolute(args.state_root);state=parse(read_bytes(root/'state.json')[0]);sequence=STAGES.index(args.stage)
    previous=None
    if sequence:
        ref=pin(root/('command-%02d.receipt.json'%(sequence-1)));previous=read_pin(ref);previous['_receipt']=ref
        need(previous['status']=='passed','A failed prior command cannot be continued')
    need(not (root/('command-%02d.receipt.json'%sequence)).exists(),'No replay/reset of completed command')
    api=GitAPI(state['site']['repository'],os.environ['GITHUB_TOKEN'])
    commit,command,wire=get_command(api,state,sequence,previous,args.wait_seconds);state['_inputCommit']=commit
    control=create(root/('command-%02d.control.json'%sequence),wire)
    before=check_sources(state);inputs=install_inputs(api,state,command['files']);actor=command['actor'];validate_actor(state,args.stage,actor)
    def fresh():validate_scope(state,command,actor,args.stage)
    fresh();admit_runtime(runtime_resources(),memory_extra=(648 if sequence==0 else 424)*M)
    outer=None;prepared=None;status='failed';failure=None;requested=actor['argv']
    try:
        if args.stage=='prepare':prepared,requested,outer=prepare(state,actor)
        else:
            config=read_pin(actor['outerConfig']);cap=root/('command-%02d-bootstrap'%sequence)
            code=cached_run(state,state['outerRunner'],['--config',actor['outerConfig']['path'],'--config-sha256',actor['outerConfig']['sha256']],cap)
            outer=pin(Path(config['outputRoot'])/'result.json');result=read_pin(outer)
            need(code==0 and result['actualExitCode']==0 and result['captureComplete']is True and result['runnerExitCode']==0,'Actual outer actor failed/incomplete')
        need(same(before,check_sources(state)),'Frozen source20/auxiliary full11 changed')
        if sequence:fresh()
        admit_runtime(runtime_resources(),memory_extra=0)
        status='passed'
    except BaseException as error:failure={'type':type(error).__name__,'errno':getattr(error,'errno',None),'actorCompletionAccepted':False}
    receipt={'schemaVersion':1,'kind':'GDAL-remote-audit-command-receipt','site':state['site'],'toolkit':state['toolkit'],
        'sequence':sequence,'stage':args.stage,'inputCommit':commit,'control':control,
        'previousReceiptSha256':None if previous is None else previous['_receipt']['sha256'],
        'actor':{'requestedArgv':requested,'plan':actor['plan'],'registration':actor['registration'],
                 'sourcePolicy':actor['sourcePolicy'],'outerResult':outer},'status':status}
    rref=seal(root/('command-%02d.receipt.json'%sequence),receipt)
    if failure:seal(root/('command-%02d.failure.json'%sequence),failure)
    # Export only Root-declared metadata trees plus the worker's own immutable
    # command/input records. Never infer a raw directory from a manifest.
    spec=command['export'];need(type(spec)is dict and set(spec)=={'roots','includeFiles'},'Export descriptor fields differ')
    allowed_roots=[];allowed_files=set(r['path']for r in source_rows(state));all_inputs=[]
    for i in range(sequence+1):
        old=read_pin(pin(root/('command-%02d.control.json'%i)));oldactor=old['actor']
        all_inputs.extend(row['remotePath']for row in old['files'])
        if i==0:
            activation=read_pin(oldactor['preparation']);allowed_roots.append(absolute(activation['outputRoot']))
        else:
            conf=read_pin(oldactor['outerConfig']);allowed_roots.append(absolute(conf['outputRoot']))
            argv=conf['requestedArgv'];output=absolute(argv[argv.index('--output')+1])
            if output.is_dir():allowed_roots.append(output)
            else:allowed_files.add(str(output))
    allowed_files.update(all_inputs)
    roots=[]
    for path in spec['roots']:
        path=absolute(path);need(path in allowed_roots,'Export root is not an approved actual metadata output');roots.append(str(path))
    for path in spec['includeFiles']:need(path in allowed_files,'Export file is not approved metadata/source/input')
    # Every completed stage remains resolvable even if a later Root descriptor
    # only repeats its own output. No arbitrary directory is inferred here.
    roots=list(dict.fromkeys([*roots,*[str(p)for p in allowed_roots if p.is_dir()]]))
    include=list(spec['includeFiles'])+all_inputs+[r['path']for r in source_rows(state)]+[str(root/'state.json'),str(root/'worker-context.json'),str(root/'job-observation.json'),str(root/'job-list-raw.json'),str(root/'runtime.json')]
    for i in range(sequence+1):
        include.extend([str(root/('command-%02d.control.json'%i)),str(root/('command-%02d.receipt.json'%i))])
        failure_path=root/('command-%02d.failure.json'%i)
        if failure_path.exists():include.append(str(failure_path))
        bootstrap=root/('prepare-capture'if i==0 else'command-%02d-bootstrap'%i)
        if bootstrap.is_dir():roots.append(str(bootstrap))
    prepared_path=root/'worker-prepared.json';pref=pin(prepared_path)if prepared_path.exists()else None
    if pref:include.append(str(prepared_path))
    gates=[]
    for i,name in enumerate(GATES,4):
        if i>sequence:break
        creceipt=parse(read_bytes(root/('command-%02d.receipt.json'%i))[0])
        if creceipt['status']!='passed':break
        plan=read_pin(creceipt['actor']['plan']);out=plan.get('gate')
        need(out==name,'Gate order differs')
        config=read_pin(read_pin(creceipt['actor']['outerResult'])['config'])
        argv=config['requestedArgv'];output=argv[argv.index('--output')+1];gates.append(pin(Path(output)/'verdict.json'))
    export_input={'site':state['site'],'toolkit':state['toolkit'],'stage':args.stage,'roots':roots,'includeFiles':list(dict.fromkeys(include)),
        'sourcePins':state['sourcePins'],'graph':{'prepared':pref,'commands':[pin(root/('command-%02d.receipt.json'%i))for i in range(sequence+1)],'gates':gates}}
    exported=export(export_input,root/'exports'/args.stage)
    print(json.dumps({'receipt':rref,'proofIndex':exported,'status':status}))
    need(status=='passed','Command failed; no subsequent command is permitted')

def preserve_failure(args,error):
    """A failed or incomplete command is diagnostic evidence, never a verdict."""
    info={'schemaVersion':1,'kind':'GDAL-remote-audit-incomplete-command',
          'stage':getattr(args,'stage','init'),'errorType':type(error).__name__,
          'errno':getattr(error,'errno',None),'completed':False}
    try:
        root=absolute(args.state_root)
        need(root.is_relative_to('/workspace/shared/gdal4k-remote-audit'),
             'Failure diagnostics cannot escape the dedicated worker namespace')
        output=root/'exports'/getattr(args,'stage','init')
        parent=parents(output,True)
        try:
            try:os.mkdir(output.name,0o700,dir_fd=parent)
            except FileExistsError:pass
            os.fsync(parent)
        finally:os.close(parent)
        target=output/'failure.json'
        if not os.path.lexists(target):seal(target,info)
    except BaseException:
        pass
    # OS-only projection cannot reveal token, signed URL, headers or input text.
    print(json.dumps(info),file=sys.stderr)

def main():
    resource.setrlimit(resource.RLIMIT_AS,(128*M,128*M))
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    i=sub.add_parser('init');i.add_argument('--kit',required=True);i.add_argument('--kit-commit',required=True);i.add_argument('--manifest-sha256',required=True);i.add_argument('--source-checkout',required=True);i.add_argument('--state-root',required=True)
    s=sub.add_parser('step');s.add_argument('--state-root',required=True);s.add_argument('--stage',choices=STAGES,required=True);s.add_argument('--wait-seconds',type=int,default=18000)
    a=p.parse_args();need(1<=getattr(a,'wait_seconds',1)<=18000,'Wait bound differs')
    try:(init if a.command=='init'else step)(a)
    except BaseException as error:
        preserve_failure(a,error)
        raise SystemExit(1)

if __name__=='__main__':main()

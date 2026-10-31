"""Pinned-cohort normal service downloads. No archive or runtime acceptance."""
import argparse,contextlib,datetime,fcntl,hashlib,json,os,re,resource,stat,sys,types
from pathlib import Path
CODE_ROOT=str(Path(__file__).resolve().parent)
TOOLS_ROOT=str(Path(CODE_ROOT).parent)
ORCH=CODE_ROOT+'/recover_cohort_batch_v1.js'
MAP_BUILDER=CODE_ROOT+'/build_cohort_materialization_map_v1.py'
POLICY_FILE=TOOLS_ROOT+'/cohort_policy_v1.py'
BASE=TEMP_ROOT=RAW=META_INPUTS=HEAD=REPOSITORY=RAW_STORAGE_ROOT=None
RAW_STORAGE_ROOTS=('/tmp','/dev/shm')
ATT='/workspace/attachments'
M=1048576
MAX_ZIP=32*M
MAX_INFLIGHT=4
WORKER_RSS=128*M
METADATA_BUDGET=8*M
RETAINED_TRANSPORT_DIRECTORY_RESERVE=M
SESSION_CLAIM_RESERVE=65536
CHUNK=65536
INPUT_CAP=64*1024
_CONFIG=None
def require(ok,why):
    if not ok:raise RuntimeError(why)

def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()

def full(s):return {k:getattr(s,k) for k in ('st_dev','st_ino','st_mode','st_nlink','st_uid','st_gid','st_size','st_atime_ns','st_mtime_ns','st_ctime_ns','st_blocks')}

def decode_full11_json(value):
    require(type(value) is str and len(value)<=4096,'opaque full11 JSON text required')
    def unique(pairs):
        out={}
        for k,v in pairs:require(k not in out,'duplicate full11 key');out[k]=v
        return out
    parsed=json.loads(value,object_pairs_hook=unique)
    require(type(parsed) is dict and set(parsed)=={'st_dev','st_ino','st_mode','st_nlink','st_uid','st_gid','st_size','st_atime_ns','st_mtime_ns','st_ctime_ns','st_blocks'},'full11 exact keys')
    require(all(type(v) is int and v>=0 for v in parsed.values()),'full11 exact nonnegative integers')
    return parsed

def compact(b):return dict(bytes=len(b),sha256=hashlib.sha256(b).hexdigest())

def open_dir(p,owner=False):
    require(os.path.isabs(p) and os.path.normpath(p)==p,'noncanonical directory')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in p.split('/')[1:]:
            nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
        if owner:require(os.fstat(fd).st_uid==os.getuid(),'directory owner')
        return fd
    except BaseException:os.close(fd);raise

def list_dir(p):
    fd=open_dir(p,True)
    try:
        noatime=os.open('.',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NOATIME,dir_fd=fd)
        try:
            before=full(os.fstat(noatime))
            with os.scandir(noatime) as it:names=sorted(e.name for e in it)
            require(full(os.fstat(noatime))==before,'directory changed during enumeration')
            return names
        finally:os.close(noatime)
    finally:os.close(fd)

def read_file(p,limit):
    parent=open_dir(os.path.dirname(p));fd=None
    try:
        s=os.stat(os.path.basename(p),dir_fd=parent,follow_symlinks=False)
        require(stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and 0<=s.st_size<=limit,'metadata file shape/size/owner')
        fd=os.open(os.path.basename(p),os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME|os.O_NONBLOCK,dir_fd=parent)
        before=full(os.fstat(fd));require(before==full(s),'metadata open identity')
        chunks=[];n=0
        while True:
            b=os.read(fd,min(CHUNK,limit-n+1))
            if not b:break
            n+=len(b);require(n<=limit,'metadata limit');chunks.append(b)
        require(full(os.fstat(fd))==before and full(os.stat(os.path.basename(p),dir_fd=parent,follow_symlinks=False))==before,'metadata changed')
        return b''.join(chunks)
    finally:
        if fd is not None:os.close(fd)
        os.close(parent)

def read_json(p,limit=2*M):return parse_json(read_file(p,limit))

def pin(p,limit=2*M):return dict(path=p,**compact(read_file(p,limit)))

def seal(p,value):
    data=(json.dumps(value,sort_keys=True,indent=2)+'\n').encode();require(len(data)<=2*M,'receipt too large')
    if p.startswith(BASE+'/'):
        # Account physical file blocks and conservative parent-directory growth
        # before every new immutable metadata write, including final-map.
        current=metadata_usage();block=os.statvfs(BASE).f_frsize
        projected=current+MAX_INFLIGHT*INPUT_CAP+((len(data)+block-1)//block)*block+65536
        require(projected<=METADATA_BUDGET,'projected metadata allocated budget')
    parent=open_dir(os.path.dirname(p),True);fd=None
    try:
        fd=os.open(os.path.basename(p),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        with os.fdopen(fd,'wb',closefd=False) as f:f.write(data);f.flush();os.fsync(fd)
        os.fchmod(fd,0o400);os.fsync(fd);os.close(fd);fd=None;os.fsync(parent)
        require(read_file(p,2*M)==data,'receipt readback')
        if p.startswith(BASE+'/'):require(metadata_usage()<=METADATA_BUDGET,'actual metadata allocated budget')
        return dict(path=p,**compact(data))
    finally:
        if fd is not None:os.close(fd)
        os.close(parent)

def ensure_child(parent,name):
    require('/' not in name and name not in ('','.','..'),'child name')
    fd=open_dir(parent,True)
    try:
        if parent==BASE or parent.startswith(BASE+'/'):require(metadata_bytes(BASE)+65536<=METADATA_BUDGET,'projected metadata directory budget')
        try:os.mkdir(name,0o700,dir_fd=fd);os.fsync(fd)
        except FileExistsError:pass
        child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        s=os.fstat(child);require(s.st_uid==os.getuid() and stat.S_IMODE(s.st_mode)==0o700,'child namespace owner/mode');os.close(child)
    finally:os.close(fd)

def metadata_bytes(p):
    s=os.lstat(p);require(stat.S_ISDIR(s.st_mode),'metadata root directory');total=s.st_blocks*512
    for n in list_dir(p):
        q=p+'/'+n;s=os.lstat(q)
        if stat.S_ISDIR(s.st_mode):total+=metadata_bytes(q)
        else:require(stat.S_ISREG(s.st_mode),'metadata namespace special entry');total+=s.st_blocks*512
    return total

def metadata_usage():
    return metadata_bytes(BASE)+(metadata_bytes(META_INPUTS) if os.path.lexists(META_INPUTS) else 0)+RETAINED_TRANSPORT_DIRECTORY_RESERVE+SESSION_CLAIM_RESERVE

def retire_metadata_input(p,expected):
    require(os.path.dirname(p)==META_INPUTS,'new input namespace')
    fd=open_dir(META_INPUTS,True)
    try:
        require(full(os.stat(os.path.basename(p),dir_fd=fd,follow_symlinks=False))==expected,'new metadata input changed before retirement')
        os.unlink(os.path.basename(p),dir_fd=fd);os.fsync(fd)
    finally:os.close(fd)

def tool_result_ok(result):
    require(isinstance(result,dict),'tool result dictionary shape')
    require(result.get('isError') is not True and not result.get('error'),'tool result error')

def validate_tool_observations(plugin,download,aid,size):
    filename='gdal-cohort-artifact-'+str(aid)+'.zip'
    require(plugin['name']=='mcp__codex_apps__github_download_workflow_artifact' and plugin['arguments']['repo_full_name']==REPOSITORY and plugin['arguments']['artifact_id']==aid and plugin['arguments'].get('file_name')==filename,'normal plugin exact arguments')
    require(download['name']=='download_file','normal local file tool')
    result=plugin['result'];local=download['result'];tool_result_ok(result);tool_result_ok(local)
    sc=result['structuredContent'];uri=sc['file_uri']
    require(sc['artifact_id']==aid and sc['file_name']==filename and sc['mime_type']=='application/zip','canonical plugin artifact identity')
    file_id=uri['file_id'];require(type(file_id) is str and re.fullmatch(r'file_[A-Za-z0-9_-]+',file_id),'canonical plugin file ID')
    require(uri['file_name']==filename and uri['mime_type']=='application/zip','canonical file metadata')
    require(download['arguments']=={'file_id':file_id},'exact returned file ID binding')
    nested=sc.get('structuredContent')
    if nested is not None:
        require(nested['artifact_id']==aid and nested['file_name']==filename and nested['mime_type']=='application/zip' and nested['file_id']==file_id and nested['file_uri']==uri,'duplicate plugin structured identity consistency')
        if 'download_url' in nested:require(nested['download_url']==uri.get('download_url'),'duplicate opaque URL consistency')
    require(type(local['size_bytes']) is int and local['size_bytes']==size,'actual download bytecount binding')
    landing=local['path'];require(type(landing) is str and os.path.normpath(landing)==landing and landing.startswith(ATT+'/'),'actual download canonical namespace')
    rel=landing[len(ATT)+1:].split('/')
    require(len(rel)==2 and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',rel[0]) and rel[1]==filename,'actual download exact basename/UUID')
    return landing

def read_small(p):
    with open(p) as f:s=f.read(65537)
    require(len(s)<=65536,'proc bound');return s

def resource_sample():
    mem={k:int(v.split()[0])*1024 for k,v in (line.split(':',1) for line in read_small('/proc/meminfo').splitlines())}
    cgmax=read_small('/sys/fs/cgroup/memory.max').strip();require(cgmax.isdecimal(),'finite cgroupv2 limit required')
    current=int(read_small('/sys/fs/cgroup/memory.current').strip());entries={}
    for line in read_small('/sys/fs/cgroup/memory.stat').splitlines():
        k,v=line.split();require(k not in entries,'duplicate memory.stat key');entries[k]=int(v)
    require('inactive_file' in entries,'missing inactive_file')
    avail={p:os.statvfs(p).f_bavail*os.statvfs(p).f_frsize for p in ['/tmp','/workspace','/dev/shm']}
    mount_types={}
    for line in read_small('/proc/self/mountinfo').splitlines():
        left,separator,right=line.partition(' - ')
        require(separator and len(left.split())>=5 and right.split(),'invalid mountinfo row')
        point=left.split()[4]
        if point in RAW_STORAGE_ROOTS:
            require(point not in mount_types,'duplicate raw storage mount')
            mount_types[point]=right.split()[0]
    rss=0
    for line in read_small('/proc/self/status').splitlines():
        if line.startswith('VmRSS:'):rss=int(line.split()[1])*1024
    return dict(atUtc=now(),hostMemAvailable=mem['MemAvailable'],cgroupMax=int(cgmax),cgroupCurrent=current,cgroupInactiveFile=entries['inactive_file'],cgroupAnon=entries.get('anon'),cgroupFile=entries.get('file'),filesystemAvailable=avail,filesystemTypes=mount_types,actualRssBytes=rss)

def check_resources(sample,remaining,metadata=0,raw_storage_root=None):
    selected=RAW_STORAGE_ROOT if raw_storage_root is None else raw_storage_root
    require(type(selected) is str and selected in RAW_STORAGE_ROOTS,'approved raw storage mount required')
    require(type(remaining) is int and remaining>=0,'remaining raw range')
    for k in ['hostMemAvailable','cgroupMax','cgroupCurrent','cgroupInactiveFile','actualRssBytes']:
        require(type(sample[k]) is int and sample[k]>=0,'invalid resource '+k)
    require(sample['cgroupMax']>0 and sample['cgroupCurrent']<=sample['cgroupMax'] and sample['cgroupInactiveFile']<=sample['cgroupCurrent'],'cgroup range')
    require(sample['actualRssBytes']<=WORKER_RSS,'worker RSS cap')
    extra=MAX_INFLIGHT*WORKER_RSS+MAX_INFLIGHT*MAX_ZIP+METADATA_BUDGET
    gross=sample['cgroupMax']-sample['cgroupCurrent']+sample['cgroupInactiveFile']
    safe=gross-128*M
    required=2048*M+remaining+extra
    require(sample['hostMemAvailable']>=required,'projected host floor')
    require(safe>=required,'projected cgroup safe floor')
    f=sample['filesystemAvailable']
    require(type(f) is dict and all(type(f.get(p)) is int and f[p]>=0 for p in ['/tmp','/workspace','/dev/shm']),'filesystem availability types')
    require(f[selected]>=2048*M+remaining,'projected selected raw filesystem floor')
    require(f['/tmp']>=2048*M,'TMP scratch floor')
    require(f['/dev/shm']>=2048*M,'SHM floor')
    if selected=='/dev/shm':
        require(type(sample.get('filesystemTypes')) is dict and sample['filesystemTypes'].get('/dev/shm')=='tmpfs','SHM raw storage must be the actual tmpfs mount')
    require(f['/workspace']>=32*M+MAX_INFLIGHT*MAX_ZIP+METADATA_BUDGET,'workspace landing floor')
    require(metadata<=METADATA_BUDGET,'metadata budget')
    return dict(rawStorageRoot=selected,cgroupGrossReclaimableHeadroom=gross,cgroupSafeHeadroom=safe,requiredProjectedMemory=required,remainingRawBytes=remaining,extraReservations=extra,requiredSelectedFilesystemBytes=2048*M+remaining,metadataAllocatedPlusRetainedTransportDirectoryReserve=metadata)

def digest_regular(parent,name,expected_size,expected_sha,expected_full=None,seal_required=False):
    require(type(expected_size) is int and 0<=expected_size<=MAX_ZIP,'official ZIP oversize')
    before=full(os.stat(name,dir_fd=parent,follow_symlinks=False))
    require(stat.S_ISREG(before['st_mode']) and before['st_uid']==os.getuid() and before['st_size']==expected_size,'raw file shape/size/owner')
    if seal_required:require(stat.S_IMODE(before['st_mode'])==0o400 and before['st_nlink']==1,'raw seal/nlink')
    if expected_full is not None:require(before==expected_full,'raw expected full11 changed')
    fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME|os.O_NONBLOCK,dir_fd=parent)
    try:
        require(full(os.fstat(fd))==before,'raw open identity');h=hashlib.sha256();n=0
        while True:
            b=os.read(fd,min(CHUNK,expected_size-n+1))
            if not b:break
            n+=len(b);require(n<=expected_size,'raw read overflow');h.update(b)
        require(n==expected_size and h.hexdigest()==expected_sha,'raw official SHA/bytes')
        require(full(os.fstat(fd))==before and full(os.stat(name,dir_fd=parent,follow_symlinks=False))==before,'raw changed during hash')
        return before
    finally:os.close(fd)

def copy_stream(src_fd,dst_fd,expected_size):
    h=hashlib.sha256();n=0
    while True:
        b=os.read(src_fd,min(CHUNK,expected_size-n+1))
        if not b:break
        n+=len(b);require(n<=expected_size,'copy overflow');h.update(b)
        view=memoryview(b)
        while view:
            k=os.write(dst_fd,view);require(k>0,'zero write');view=view[k:]
    return n,h.hexdigest()

def promote(landing,rawdir,name,size,sha,expected_landing):
    require(type(size) is int and 0<=size<=MAX_ZIP,'official ZIP oversize')
    srcparent=open_dir(os.path.dirname(landing),True);dstparent=open_dir(rawdir,True)
    srcfd=None;dstfd=None;incoming=name+'.incoming'
    try:
        try:os.stat(name,dir_fd=dstparent,follow_symlinks=False)
        except FileNotFoundError:pass
        else:raise RuntimeError('preexisting destination')
        before=full(os.stat(os.path.basename(landing),dir_fd=srcparent,follow_symlinks=False))
        require(before==expected_landing,'landing identity changed')
        require(stat.S_ISREG(before['st_mode']) and before['st_uid']==os.getuid() and before['st_nlink']==1 and before['st_size']==size,'landing shape/bytes/owner/nlink')
        srcfd=os.open(os.path.basename(landing),os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME|os.O_NONBLOCK,dir_fd=srcparent)
        require(full(os.fstat(srcfd))==before,'landing opened identity')
        dstfd=os.open(incoming,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=dstparent)
        n,digest=copy_stream(srcfd,dstfd,size)
        require(n==size and digest==sha,'copy official SHA/bytes')
        require(full(os.fstat(srcfd))==before and full(os.stat(os.path.basename(landing),dir_fd=srcparent,follow_symlinks=False))==before,'landing changed during copy')
        os.fsync(dstfd);os.fchmod(dstfd,0o400);os.fsync(dstfd);os.close(dstfd);dstfd=None
        # link is an atomic no-overwrite promotion; the temporary name is ours.
        digest_regular(dstparent,incoming,size,sha,seal_required=True)
        os.link(incoming,name,src_dir_fd=dstparent,dst_dir_fd=dstparent,follow_symlinks=False)
        os.unlink(incoming,dir_fd=dstparent);os.fsync(dstparent)
        destfull=digest_regular(dstparent,name,size,sha,seal_required=True)
        require(full(os.fstat(srcfd))==before,'landing changed after promotion')
        return dict(path=rawdir+'/'+name,bytes=size,sha256=sha,full11=destfull,landingFull11=before)
    finally:
        if srcfd is not None:os.close(srcfd)
        if dstfd is not None:os.close(dstfd)
        os.close(srcparent);os.close(dstparent)

@contextlib.contextmanager
def lock():
    p=CODE_ROOT+'/receiver.lock';fd=os.open(p,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        s=os.fstat(fd);require(stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and s.st_nlink==1 and s.st_size==0,'lock shape')
        fcntl.flock(fd,fcntl.LOCK_EX)
        if _CONFIG is not None and os.path.lexists(BASE+'/plan-v1.json'):verify_session_claim()
        yield
    finally:os.close(fd)

def state(plan):
    leases=set(list_dir(BASE+'/leases'));done=set(list_dir(BASE+'/completed'))
    require(all(re.fullmatch(r'\d+\.json',n) for n in leases|done),'state namespace unexpected')
    require(done<=leases,'completion without lease');require(leases<={str(a['id'])+'.json' for a in plan['missingArtifacts']},'unselected state entry')
    active=leases-done;require(len(active)<=MAX_INFLIGHT,'inflight cap')
    remaining=sum(a['size_in_bytes'] for a in plan['missingArtifacts'] if str(a['id'])+'.json' not in done)
    return active,done,remaining

def admit(artifact_id):
    plan,planpin=load_plan()
    with lock():
        for n in ['leases','completed','promotions','retirements','transports','operations']:ensure_child(BASE,n)
        # Initialization and every later shared metadata mutation use this flock.
        try:os.mkdir(TEMP_ROOT,0o700)
        except FileExistsError:pass
        ensure_child(TEMP_ROOT,'raw')
        ensure_child(TEMP_ROOT,'metadata-inputs')
        active,done,remaining=state(plan);name=str(artifact_id)+'.json'
        require(name not in active|done,'artifact already leased/completed');require(len(active)<MAX_INFLIGHT,'inflight cap')
        art=next((a for a in plan['missingArtifacts'] if a['id']==artifact_id),None);require(art is not None,'unselected artifact')
        require(not os.path.lexists(RAW+'/'+str(artifact_id)+'.zip') and not os.path.lexists(RAW+'/'+str(artifact_id)+'.zip.incoming'),'preexisting raw destination/incoming')
        sample=resource_sample();qualification=check_resources(sample,remaining,metadata_usage()+MAX_INFLIGHT*INPUT_CAP+65536)
        lease=dict(schemaVersion=1,kind='GDAL-cohort-new-normal-download-lease',artifactId=artifact_id,createdAtUtc=now(),plan=planpin,officialArtifact=art,rawDestination=RAW+'/'+str(artifact_id)+'.zip',resources=dict(sample=sample,qualification=qualification),activeBeforeAdmission=len(active))
        print(json.dumps(seal(BASE+'/leases/'+name,lease)))

def stage_input(artifact_id,stage,payload):
    require(stage in ['plugin','file','transport'] and type(payload) is str,'new metadata staging arguments')
    data=payload.encode('utf-8');require(0<len(data)<=INPUT_CAP,'new metadata input bound')
    plan,planpin=load_plan();path=META_INPUTS+'/'+str(artifact_id)+'-'+stage+'.json'
    with lock():
        active,done,remaining=state(plan);name=str(artifact_id)+'.json';require(name in active and name not in done,'staging active lease')
        sample=resource_sample();check_resources(sample,remaining,metadata_usage()+MAX_INFLIGHT*INPUT_CAP+65536)
        parent=open_dir(META_INPUTS,True)
        try:
            fd=os.open(os.path.basename(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
            with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
            os.fsync(parent)
        finally:os.close(parent)
        require(metadata_usage()<=METADATA_BUDGET,'actual staging metadata budget')
        print(json.dumps(dict(path=path,**compact(data))))

def receive(transport_path):
    plan,planpin=load_plan();transport=read_json(transport_path,INPUT_CAP)
    require(transport['kind']=='GDAL-cohort-normal-artifact-download-observation','transport kind')
    aid=transport['artifactId'];require(type(aid) is int,'artifact ID type');name=str(aid)+'.json'
    require(transport_path==BASE+'/transports/'+name,'transport receipt path')
    with lock():
        active,done,remaining=state(plan);require(name in active and name not in done,'no active lease')
        lease=read_json(BASE+'/leases/'+name);require(lease['plan']==planpin and lease['artifactId']==aid,'lease binding')
        art=lease['officialArtifact'];require(art in plan['missingArtifacts'],'artifact plan binding')
        plugin=transport['pluginTool'];download=transport['fileDownloadTool']
        landing=validate_tool_observations(plugin,download,aid,art['size_in_bytes'])
        require(landing==transport['landingPath'],'exact local tool returned landing path')
        rel=landing[len(ATT)+1:].split('/');require(len(rel)==2 and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',rel[0]),'landing UUID shape')
        require(rel[1]=='gdal-cohort-artifact-'+str(aid)+'.zip','exact landing basename')
        require(rel[0] not in plan['initialAttachmentNames'],'preexisting attachment parent')
        require(list_dir(ATT+'/'+rel[0])==[rel[1]],'landing parent exact entry')
        require(os.lstat(ATT+'/'+rel[0]).st_blocks*512<=os.statvfs(ATT).f_frsize,'new landing directory allocated block bound')
        require(transport['pluginObservation']==pin(BASE+'/operations/'+str(aid)+'-plugin.json') and read_json(BASE+'/operations/'+str(aid)+'-plugin.json')['operation']==plugin,'sealed actual plugin observation binding')
        require(transport['fileDownloadObservation']==pin(BASE+'/operations/'+str(aid)+'-file.json') and read_json(BASE+'/operations/'+str(aid)+'-file.json')['operation']==download,'sealed actual local download observation binding')
        require(transport['orchestratorSource']==plan['orchestratorSource'],'transport orchestrator source binding')
        require('landingFull11' not in transport,'rounded full11 object forbidden')
        landing_full11=decode_full11_json(transport['landingFull11JSON'])
        sample=resource_sample();qualification=check_resources(sample,remaining,metadata_usage())
        result=promote(landing,RAW,str(aid)+'.zip',art['size_in_bytes'],art['digest'][7:],landing_full11)
        promotion=dict(schemaVersion=1,kind='GDAL-cohort-new-current-raw-promotion',artifactId=aid,atUtc=now(),plan=planpin,lease=pin(BASE+'/leases/'+name),transport=pin(transport_path,INPUT_CAP),officialArtifact=art,materialization=result,resources=dict(sample=sample,qualification=qualification),noFamilyAcceptanceClaim=True)
        proof=seal(BASE+'/promotions/'+name,promotion)
        intent=seal(BASE+'/retirements/'+name,dict(schemaVersion=1,kind='GDAL-cohort-new-tool-landing-retirement-intent',atUtc=now(),artifactId=aid,promotion=proof,landingPath=landing,expectedFull11=landing_full11,scope='Only exact new tool-returned transport file; no old attachment/raw cleanup'))
        parent=open_dir(os.path.dirname(landing),True)
        try:
            require(full(os.stat(rel[1],dir_fd=parent,follow_symlinks=False))==landing_full11,'landing changed before exact retirement')
            os.unlink(rel[1],dir_fd=parent);os.fsync(parent)
            try:os.stat(rel[1],dir_fd=parent,follow_symlinks=False)
            except FileNotFoundError:pass
            else:raise RuntimeError('landing retirement failed')
        finally:os.close(parent)
        completed=seal(BASE+'/completed/'+name,dict(schemaVersion=1,kind='GDAL-cohort-new-current-raw-completed',artifactId=aid,atUtc=now(),promotion=proof,retirementIntent=intent,landingRetired=True,materialization=result,noFamilyAcceptanceClaim=True))
        print(json.dumps(completed))

def seal_transport(input_path,artifact_id):
    require(input_path==META_INPUTS+'/'+str(artifact_id)+'-transport.json','exact new transport input path')
    input_full=full(os.lstat(input_path));require(stat.S_ISREG(input_full['st_mode']) and input_full['st_nlink']==1,'new transport input shape')
    plan,planpin=load_plan();value=read_json(input_path,INPUT_CAP)
    require(value['artifactId']==artifact_id and value['kind']=='GDAL-cohort-normal-artifact-download-observation','transport input binding')
    with lock():
        active,done,remaining=state(plan);name=str(artifact_id)+'.json';require(name in active and name not in done,'transport active lease')
        sample=resource_sample();check_resources(sample,remaining,metadata_usage())
        result=seal(BASE+'/transports/'+name,value);retire_metadata_input(input_path,input_full);print(json.dumps(result))

def seal_operation(input_path,artifact_id,stage):
    require(stage in ['plugin','file'] and input_path==META_INPUTS+'/'+str(artifact_id)+'-'+stage+'.json','exact new operation input path')
    input_full=full(os.lstat(input_path));require(stat.S_ISREG(input_full['st_mode']) and input_full['st_nlink']==1,'new operation input shape')
    plan,planpin=load_plan();value=read_json(input_path,INPUT_CAP)
    require(stage in ['plugin','file'] and value['artifactId']==artifact_id and value['kind']=='GDAL-cohort-actual-normal-download-tool-operation' and value['stage']==stage,'operation input binding')
    require(value['orchestratorSource']==plan['orchestratorSource'],'operation orchestrator source binding')
    with lock():
        active,done,remaining=state(plan);name=str(artifact_id)+'.json';require(name in active and name not in done,'operation active lease')
        sample=resource_sample();check_resources(sample,remaining,metadata_usage())
        result=seal(BASE+'/operations/'+str(artifact_id)+'-'+stage+'.json',value);retire_metadata_input(input_path,input_full);print(json.dumps(result))

def parse_json(raw):
    def unique(rows):
        out={}
        for key,value in rows:require(key not in out,'duplicate JSON key');out[key]=value
        return out
    return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(RuntimeError('nonfinite JSON')))
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def integer(value,minimum=1):return type(value) is int and minimum<=value<=2**53-1
def reference_shape(value):
    require(type(value) is dict and set(value)=={'path','bytes','sha256'},'exact reference fields')
    require(type(value['path']) is str and os.path.isabs(value['path']) and os.path.normpath(value['path'])==value['path'],'reference absolute path')
    require(integer(value['bytes'],0) and value['bytes']<=2*M and type(value['sha256']) is str and re.fullmatch('[0-9a-f]{64}',value['sha256']),'reference primitive types')
    return value
def reference(value):
    reference_shape(value)
    require(pin(value['path'])==value,'reference bytes/SHA')
    return value
def read_pinned_json(ref):
    reference(ref)
    raw=read_file(ref['path'],2*M)
    require(compact(raw)=={'bytes':ref['bytes'],'sha256':ref['sha256']},'exact parsed JSON bytes/SHA')
    return parse_json(raw)
def validate_catalogue(cat,intent,manifest):
    require(type(cat) is dict and type(cat.get('schemaVersion')) is int and cat['schemaVersion']==1 and cat.get('kind')=='GDAL-cohort-official-artifact-catalogue','catalogue kind')
    scope=intent['scope']
    reference_shape(cat.get('cohortManifest'))
    require(canonical(cat['cohortManifest'])==canonical(manifest) and cat.get('repository')==scope['repository'] and cat.get('head')==scope['head'],'catalogue current intent identity')
    runs=cat.get('runs');expected={scope['primaryRun'],scope['profileRun']}
    require(type(runs) is list and len(runs)==2 and all(type(r) is dict and integer(r.get('id')) and integer(r.get('attempt')) and r.get('status')=='completed' and r.get('conclusion')=='success' and r.get('head')==scope['head'] for r in runs),'current complete successful typed runs')
    require({r['id'] for r in runs}==expected,'current run IDs')
    official=cat.get('officialArtifacts');selected=cat.get('selectedArtifacts');families=cat.get('logicalFamilies')
    require(type(official) is list and type(selected) is list and 0<len(selected)<=4096 and type(families) is list,'catalogue arrays')
    byid={}
    for a in official:
        require(type(a) is dict and integer(a.get('id')) and a['id'] not in byid and integer(a.get('size_in_bytes'),0) and type(a.get('name')) is str and a['name'],'official artifact primitives/duplicates')
        wf=a.get('workflow_run',{});require(integer(wf.get('id')) and wf['id'] in expected and wf.get('head_sha')==scope['head'] and integer(wf.get('repository_id')) and wf['repository_id']==scope['repositoryId'],'official artifact current source/run')
        byid[a['id']]=a
    chosen={}
    for a in selected:
        require(type(a) is dict and integer(a.get('id')) and a['id'] not in chosen and a['id'] in byid and canonical(a)==canonical(byid[a['id']]),'selected raw official metadata binding')
        require(integer(a.get('size_in_bytes')) and a['size_in_bytes']<=MAX_ZIP and a.get('expired') is False and type(a.get('digest')) is str and re.fullmatch('sha256:[0-9a-f]{64}',a['digest']),'selected official size/expiry/digest')
        chosen[a['id']]=a
    names=[];partition=[]
    for family in families:
        require(type(family) is dict and set(family)=={'original','route','selected'},'logical family fields')
        original=family['original'];require(type(original) is dict and integer(original.get('id')) and original['id'] in byid and canonical(original)==canonical(byid[original['id']]),'logical original metadata')
        require(family['route'] in ('original-service-zip','audit-repack') and type(family['selected']) is list and family['selected'] and all(integer(x) and x in chosen for x in family['selected']),'logical selection route/IDs')
        if family['route']=='original-service-zip':require(family['selected']==[original['id']],'direct family identity')
        names.append(original['name']);partition.extend(family['selected'])
    require(len(names)==len(set(names)) and set(names)==set(intent['familyNames']),'workflow-derived logical family names')
    require(len(partition)==len(set(partition))==len(chosen) and set(partition)==set(chosen),'exact selected partition')
    return selected

def validate_scope(scope,intent,cat,approved_at,catalogue_ref,manifest_ref):
    reference_shape(scope.get('catalogue'));reference_shape(scope.get('cohortManifest'))
    reference_shape(catalogue_ref);reference_shape(manifest_ref)
    require(canonical(scope['catalogue'])==canonical(catalogue_ref) and canonical(scope['cohortManifest'])==canonical(manifest_ref),'qualified scope exact catalogue/intent references')
    s=intent['scope']
    require(scope.get('kind')=='GDAL-cohort-qualified-current-scope' and all(scope.get(k)==s[k] for k in ('head','tree','base','testCheckout')),'activation qualified current identity')
    require(scope.get('checkoutTree')==s['tree'] and scope.get('checkoutParents')==[s['base'],s['head']],'activation checkout')
    rows=scope.get('requiredRoleChecks');names={name:role for role,group in intent['ci']['requiredRoles'].items() for name in group}
    runs={r['id']:r for r in cat['runs']};require(type(rows) is list and len(rows)==len(names) and {r.get('name') for r in rows}==set(names),'all current required roles')
    require(len({r.get('id') for r in rows})==len(rows),'duplicate current job')
    for r in rows:
        rid=s['primaryRun'] if names[r['name']]=='primary' else s['profileRun']
        require(integer(r.get('id')) and integer(r.get('runId')) and r['runId']==rid and type(r.get('attempt')) is int and r['attempt']==runs[rid]['attempt'] and r.get('status')=='completed' and r.get('conclusion')=='success' and r.get('head')==s['head'],'current successful role/run/attempt')
    parse=lambda value:datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
    start,end,expiry,approved=map(parse,[scope['observedStartedAtUtc'],scope['observedCompletedAtUtc'],scope['expiresAtUtc'],approved_at])
    require(start<=end<=approved<=expiry and 0<(expiry-start).total_seconds()<=intent['ci']['maximumAgeSeconds'],'activation within genuine scope lease')

def add_arguments(parser):
    parser.add_argument('--cohort-manifest',required=True)
    parser.add_argument('--cohort-manifest-sha256',required=True)
    parser.add_argument('--activation',required=True)
    parser.add_argument('--activation-sha256',required=True)
    parser.add_argument('--session-root',required=True)

def namespace_paths(manifest_sha,session,storage_root,raw_root):
    require(type(manifest_sha) is str and re.fullmatch('[0-9a-f]{64}',manifest_sha),'manifest SHA type')
    require(type(session) is str and os.path.normpath(session)==session and os.path.dirname(session)==CODE_ROOT+'/sessions' and re.fullmatch('[a-z0-9][a-z0-9-]{0,63}',os.path.basename(session)),'new controlled session namespace')
    require(type(storage_root) is str and storage_root in RAW_STORAGE_ROOTS,'approved raw storage mount required')
    temp=storage_root+'/gdal-cohort-'+manifest_sha[:16]+'-'+os.path.basename(session)
    require(type(raw_root) is str and raw_root==temp+'/raw','activation raw namespace/mount binding')
    return temp,temp+'/raw',temp+'/metadata-inputs'

def initialize(args):
    global _CONFIG,BASE,TEMP_ROOT,RAW,META_INPUTS,HEAD,REPOSITORY,RAW_STORAGE_ROOT
    manifest=dict(path=args.cohort_manifest,bytes=os.lstat(args.cohort_manifest).st_size,sha256=args.cohort_manifest_sha256)
    reference(manifest)
    activation_ref=dict(path=args.activation,bytes=os.lstat(args.activation).st_size,sha256=args.activation_sha256);reference(activation_ref)
    require(stat.S_IMODE(os.lstat(args.activation).st_mode)==0o400,'sealed root activation required')
    activation=read_pinned_json(activation_ref)
    expected={'schemaVersion','kind','approvedBy','approvedAtUtc','approvedForNormalDownloads','cohortManifest','sourceCatalogue','qualifiedScope','sessionRoot','rawStorageRoot','rawRoot','sources','retainedArtifacts'}
    require(type(activation) is dict and set(activation)==expected and type(activation['schemaVersion']) is int and activation['schemaVersion']==1 and activation['kind']=='GDAL-cohort-normal-download-source-activation' and activation['approvedBy']=='root' and activation['approvedForNormalDownloads'] is True,'exact root activation required')
    reference_shape(activation['cohortManifest']);require(canonical(activation['cohortManifest'])==canonical(manifest),'activation intent pin')
    sources=activation['sources'];require(type(sources) is list and len(sources)==4,'receiver source closure')
    require({r['path'] for r in sources}=={__file__,ORCH,MAP_BUILDER,POLICY_FILE},'exact receiver source files')
    for r in sources:reference(r);require(stat.S_IMODE(os.lstat(r['path']).st_mode)==0o400,'root-frozen source required')
    fd=os.open(POLICY_FILE,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
    with os.fdopen(fd,'rb') as f:body=f.read(2*M+1)
    policy_ref=next(r for r in sources if r['path']==POLICY_FILE)
    require(compact(body)=={'bytes':policy_ref['bytes'],'sha256':policy_ref['sha256']},'exact executed policy source bytes')
    policy=types.ModuleType('receiver_pinned_cohort_policy');policy.__file__=POLICY_FILE;exec(compile(body,POLICY_FILE,'exec'),policy.__dict__)
    intent=policy.install(manifest)
    cat=read_pinned_json(activation['sourceCatalogue']);selected=validate_catalogue(cat,intent,manifest)
    qualified_scope=read_pinned_json(activation['qualifiedScope'])
    validate_scope(qualified_scope,intent,cat,activation['approvedAtUtc'],activation['sourceCatalogue'],manifest)
    session=args.session_root
    temp,raw,metadata_inputs=namespace_paths(manifest['sha256'],session,activation['rawStorageRoot'],activation['rawRoot'])
    require(activation['sessionRoot']==session,'activation session namespace binding')
    config=dict(manifest=manifest,activation=activation,activationRef=activation_ref,intent=intent,selected=selected,qualifiedScope=qualified_scope)
    if _CONFIG is not None:require(canonical(config)==canonical(_CONFIG),'process cannot switch receiver activation');return _CONFIG
    BASE=session;TEMP_ROOT=temp;RAW=raw;META_INPUTS=metadata_inputs;RAW_STORAGE_ROOT=activation['rawStorageRoot'];HEAD=intent['scope']['head'];REPOSITORY=intent['scope']['repository'];_CONFIG=config
    return config

def session_claim():
    return dict(schemaVersion=1,kind='GDAL-cohort-single-normal-download-session',cohortManifest=_CONFIG['manifest'],activation=_CONFIG['activationRef'],sessionRoot=BASE,rawStorageRoot=RAW_STORAGE_ROOT,rawRoot=RAW)
def verify_session_claim():
    require(canonical(read_json(CODE_ROOT+'/session-claim-v1.json'))==canonical(session_claim()),'immutable single receiver session claim differs')

def load_plan():
    require(_CONFIG is not None,'explicit root activation required')
    verify_session_claim()
    p=BASE+'/plan-v1.json';plan=read_json(p)
    require(type(plan.get('schemaVersion')) is int and plan['schemaVersion']==1 and plan['kind']=='GDAL-cohort-normal-download-plan' and plan['head']==HEAD and plan['repository']==REPOSITORY and plan['rawDestination']==RAW,'plan identity')
    for key in ('cohortManifest','activation','sourceCatalogue','receiverSource','orchestratorSource','mapBuilderSource','policySource'):reference_shape(plan[key])
    require(canonical(plan['cohortManifest'])==canonical(_CONFIG['manifest']) and canonical(plan['activation'])==canonical(_CONFIG['activationRef']) and canonical(plan['sourceCatalogue'])==canonical(_CONFIG['activation']['sourceCatalogue']),'plan activation/current catalogue pins')
    require(plan['receiverSource']==pin(__file__) and plan['orchestratorSource']==pin(ORCH) and plan['mapBuilderSource']==pin(MAP_BUILDER) and plan['policySource']==pin(POLICY_FILE),'frozen execution source pins')
    selected=_CONFIG['selected'];retained=_CONFIG['activation']['retainedArtifacts']
    require(canonical(plan['retainedArtifacts'])==canonical(retained) and canonical(plan['missingArtifacts'])==canonical([a for a in selected if str(a['id']) not in retained]),'plan selected/retained partition')
    return plan,pin(p)

def prepare():
    require(_CONFIG is not None,'root activation required')
    with lock():
        parse=lambda value:datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
        require(parse(_CONFIG['activation']['approvedAtUtc'])<=parse(now())<=parse(_CONFIG['qualifiedScope']['expiresAtUtc']),'initial session activation requires unexpired qualified scope')
        require(not os.path.lexists(CODE_ROOT+'/session-claim-v1.json'),'receiver source already owns an immutable session; no second session/reset')
        require(not os.path.lexists(BASE) and not os.path.lexists(TEMP_ROOT),'fresh metadata and tmp namespaces required; no orphan adoption')
        retained_initial=_CONFIG['activation']['retainedArtifacts'];require(type(retained_initial) is dict,'explicit retained map')
        initial_remaining=sum(a['size_in_bytes'] for a in _CONFIG['selected'] if str(a['id']) not in retained_initial)
        check_resources(resource_sample(),initial_remaining,RETAINED_TRANSPORT_DIRECTORY_RESERVE+SESSION_CLAIM_RESERVE+MAX_INFLIGHT*INPUT_CAP+65536)
        claim=session_claim();require(len(canonical(claim).encode())+4096<=SESSION_CLAIM_RESERVE,'session claim physical reservation')
        seal(CODE_ROOT+'/session-claim-v1.json',claim)
        ensure_child(CODE_ROOT,'sessions');ensure_child(CODE_ROOT+'/sessions',os.path.basename(BASE))
        os.mkdir(TEMP_ROOT,0o700);ensure_child(TEMP_ROOT,'raw');ensure_child(TEMP_ROOT,'metadata-inputs')
        for name in ['leases','completed','promotions','retirements','transports','operations']:ensure_child(BASE,name)
        retained=_CONFIG['activation']['retainedArtifacts'];require(type(retained) is dict,'explicit retained map')
        selected={str(a['id']):a for a in _CONFIG['selected']};require(set(retained)<=set(selected),'retained current selected IDs only')
        for key,value in retained.items():
            a=selected[key];require(key==str(a['id']) and set(value)=={'artifactId','path','bytes','sha256','full11','receipt','materialization'} and type(value['artifactId']) is int and value['artifactId']==a['id'] and type(value['bytes']) is int and value['bytes']==a['size_in_bytes'] and value['sha256']==a['digest'][7:] and value['materialization']=='retained-byte-identical-current-official','retained explicit current official identity')
            require(type(value['full11']) is dict and set(value['full11'])==set(full(os.lstat(__file__))) and all(type(x) is int and x>=0 for x in value['full11'].values()),'retained exact Python full11')
            reference(value['receipt']);parent=open_dir(os.path.dirname(value['path']),True)
            try:digest_regular(parent,os.path.basename(value['path']),a['size_in_bytes'],a['digest'][7:],value['full11'],True)
            finally:os.close(parent)
        missing=[a for a in _CONFIG['selected'] if str(a['id']) not in retained]
        block=os.statvfs(ATT).f_frsize
        require((len(missing)+1)*block+65536<=RETAINED_TRANSPORT_DIRECTORY_RESERVE,'retained landing directory reserve')
        require(all(((a['size_in_bytes']+block-1)//block)*block+block<=MAX_ZIP for a in missing),'physical landing plus directory bound')
        plan=dict(schemaVersion=1,kind='GDAL-cohort-normal-download-plan',createdAtUtc=now(),head=HEAD,repository=REPOSITORY,cohortManifest=_CONFIG['manifest'],activation=_CONFIG['activationRef'],rawDestination=RAW,sourceCatalogue=_CONFIG['activation']['sourceCatalogue'],receiverSource=pin(__file__),orchestratorSource=pin(ORCH),mapBuilderSource=pin(MAP_BUILDER),policySource=pin(POLICY_FILE),missingArtifacts=missing,retainedArtifacts=retained,initialAttachmentNames=list_dir(ATT),noFamilyAcceptanceClaim=True)
        sample=resource_sample();plan['initialResources']=dict(sample=sample,qualification=check_resources(sample,sum(a['size_in_bytes'] for a in missing),metadata_usage()))
        print(json.dumps(seal(BASE+'/plan-v1.json',plan)))

def batch_info():
    plan,proof=load_plan()
    with lock():
        active,done,remaining=state(plan)
        eligible=[a['id'] for a in plan['missingArtifacts'] if str(a['id'])+'.json' not in active|done]
        print(json.dumps(dict(plan=proof,repository=REPOSITORY,metadataInputs=META_INPUTS,orchestratorSource=plan['orchestratorSource'],selectedForNewDownload=eligible,activeArtifactIds=sorted(int(x[:-5]) for x in active),completedArtifactIds=sorted(int(x[:-5]) for x in done),cohortManifest=_CONFIG['manifest'])))

def main():
    q=argparse.ArgumentParser();add_arguments(q)
    q.add_argument('command',choices=['prepare','admit','batch-info','stage-input','seal-operation','seal-transport','receive'])
    q.add_argument('--artifact-id',type=int);q.add_argument('--transport');q.add_argument('--input');q.add_argument('--stage');q.add_argument('--input-json')
    a=q.parse_args();resource.setrlimit(resource.RLIMIT_AS,(WORKER_RSS,WORKER_RSS));initialize(a)
    if a.command=='prepare':prepare()
    elif a.command=='batch-info':batch_info()
    elif a.command=='admit':require(integer(a.artifact_id),'artifact ID required');admit(a.artifact_id)
    elif a.command=='stage-input':require(integer(a.artifact_id) and a.stage is not None and a.input_json is not None,'staging arguments');stage_input(a.artifact_id,a.stage,a.input_json)
    elif a.command=='seal-operation':seal_operation(a.input,a.artifact_id,a.stage)
    elif a.command=='seal-transport':seal_transport(a.input,a.artifact_id)
    else:receive(a.transport)
if __name__=='__main__':main()

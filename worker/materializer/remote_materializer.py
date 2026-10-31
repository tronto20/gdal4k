"""Prepare same-runner official service ZIPs from genuine, pinned Root inputs.

No predicate imports, MCP records, retries, overwrite, SDK execution or acceptance.
Bearer credentials are attached only to canonical api.github.com requests.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import resource
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

M = 1048576
CHUNK = 65536
FIELDS = ('st_dev','st_ino','st_mode','st_nlink','st_uid','st_gid','st_size',
          'st_atime_ns','st_mtime_ns','st_ctime_ns','st_blocks')
ACTIVATION_KEYS = {'schemaVersion','kind','rootApprovedPrepare','repository','head','tree','base',
                   'auditRunId','auditRunAttempt','auditJobId','cohortManifest','sourceCatalogue',
                   'qualifiedScope','materializerSource','rawRoot','outputRoot'}

class MaterializationError(RuntimeError):
    def __init__(self, code, partial=None):
        super().__init__(code)
        self.code, self.partial = code, partial
        self.http = None

def need(condition, code):
    if not condition:
        raise MaterializationError(code)

def full(value):
    return {key:getattr(value,key) for key in FIELDS}

def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00','Z')

def timestamp(value):
    need(isinstance(value,str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z',value),'UTC_TIMESTAMP')
    return dt.datetime.fromisoformat(value[:-1]+'+00:00')

def unique(pairs):
    value={}
    for key,item in pairs:
        need(key not in value,'DUPLICATE_JSON_KEY'); value[key]=item
    return value

def parse(raw):
    return json.loads(raw,object_pairs_hook=unique,
                      parse_constant=lambda _:(_ for _ in ()).throw(MaterializationError('NONFINITE_JSON')))

def parent_fd(path):
    need(isinstance(path,str) and path.startswith('/') and os.path.normpath(path)==path and not path.startswith('//'),'ABSOLUTE_PATH')
    parts=path.split('/')[1:]; directory=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            following=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            os.close(directory); directory=following
        return directory,parts[-1]
    except BaseException:
        os.close(directory); raise

def read_pin(reference):
    need(type(reference) is dict and set(reference)=={'path','bytes','sha256'} and
         type(reference['bytes']) is int and 0<=reference['bytes']<=2*M and
         re.fullmatch('[0-9a-f]{64}',reference['sha256']) is not None,'REFERENCE_SCHEMA')
    directory,name=parent_fd(reference['path'])
    try:fd=os.open(name,os.O_RDONLY|os.O_NOATIME|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
    finally:os.close(directory)
    try:
        before=full(os.fstat(fd)); need(stat.S_ISREG(before['st_mode']) and before['st_uid']==os.getuid() and
            before['st_nlink']==1 and stat.S_IMODE(before['st_mode'])==0o400 and before['st_size']==reference['bytes'],'PIN_SEAL')
        raw=os.read(fd,reference['bytes']+1)
        need(len(raw)==reference['bytes'] and hashlib.sha256(raw).hexdigest()==reference['sha256'],'PIN_BYTES_SHA')
        need(full(os.fstat(fd))==before==full(os.lstat(reference['path'])),'PIN_FULL11_CHANGED')
        return raw,before
    finally:os.close(fd)

def cli_ref(pair):
    path,sha=pair; return dict(path=path,bytes=os.lstat(path).st_size,sha256=sha)

def make_directory(path):
    directory,name=parent_fd(path)
    try:
        info=os.fstat(directory)
        need(info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o700,'PRIVATE_PARENT_REQUIRED')
        os.mkdir(name,0o700,dir_fd=directory); os.fsync(directory)
        return os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
    finally:os.close(directory)

def write_all(fd,data):
    view=memoryview(data)
    while view:
        count=os.write(fd,view); need(count>0,'SHORT_WRITE'); view=view[count:]

def seal_json(directory,path,value):
    raw=(json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
    need(len(raw)<=2*M,'OUTPUT_METADATA_BOUND')
    fd=os.open(os.path.basename(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
    try:write_all(fd,raw); os.fsync(fd); os.fchmod(fd,0o400); os.fsync(fd)
    finally:os.close(fd)
    os.fsync(directory)
    return dict(path=path,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())

def artifact_identity(item):
    need(type(item) is dict and type(item.get('id')) is int and item['id']>0 and
         type(item.get('name')) is str and 0<len(item['name'])<=512 and
         type(item.get('size_in_bytes')) is int and 0<item['size_in_bytes']<=32*M and
         type(item.get('digest')) is str and re.fullmatch('sha256:[0-9a-f]{64}',item['digest']) is not None and
         item.get('expired') is False and type(item.get('workflow_run')) is dict,'ARTIFACT_METADATA_SCHEMA')
    run=item['workflow_run']
    need(type(run.get('id')) is int and run['id']>0 and re.fullmatch('[0-9a-f]{40}',run.get('head_sha','')) is not None,'ARTIFACT_RUN_SCHEMA')
    return dict(id=item['id'],name=item['name'],bytes=item['size_in_bytes'],digest=item['digest'],
                runId=run['id'],head=run['head_sha'],expired=False)

def verify_artifact(detail,expected,scope):
    need(artifact_identity(detail)==artifact_identity(expected),'CURRENT_ARTIFACT_IDENTITY_CHANGED')
    run=detail['workflow_run']
    need(run.get('repository_id')==scope['repositoryId'] and run.get('head_repository_id')==scope['repositoryId'] and
         run.get('head_branch')==scope['branch'],'CURRENT_ARTIFACT_REPOSITORY_BRANCH')

def qualified_location(url):
    try:
        value=urllib.parse.urlsplit(url)
        host=value.hostname or ''
        return (value.scheme=='https' and not value.username and not value.password and not value.fragment and
                value.port in (None,443) and len(url)<=16384 and value.path.startswith('/') and
                (re.fullmatch(r'[a-z0-9][a-z0-9-]*\.blob\.core\.windows\.net',host) is not None or
                 re.fullmatch(r'[a-z0-9][a-z0-9.-]*\.actions\.githubusercontent\.com',host) is not None))
    except ValueError:return False

def http_summary(url,response,kind,authorized):
    parsed=urllib.parse.urlsplit(url); length=response.headers.get('Content-Length')
    media=response.headers.get('Content-Type','').split(';',1)[0].lower()
    return dict(kind=kind,origin=parsed.scheme+'://'+parsed.netloc,status=int(response.status),
                contentLength=int(length) if length is not None and re.fullmatch('[0-9]{1,12}',length) else None,
                mediaType=media if re.fullmatch('[a-z0-9.+_-]+/[a-z0-9.+_-]+',media) else None,
                authorizationSent=authorized)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None

class RestClient:
    def __init__(self,repository,token,opener=None):
        need(re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository) is not None,'REPOSITORY_NAME')
        need(isinstance(token,str) and token and '\r' not in token and '\n' not in token,'TOKEN_ENV_REQUIRED')
        self.repository,self.token=repository,token
        self.opener=opener or urllib.request.build_opener(NoRedirect())
    def api_url(self,path):
        return 'https://api.github.com/repos/'+self.repository+path
    def response(self,url,api):
        # No automatic redirect can carry this header to a storage origin.
        need((url.startswith('https://api.github.com/repos/'+self.repository+'/') and
              urllib.parse.urlsplit(url).netloc=='api.github.com') if api else qualified_location(url),'HTTP_ORIGIN')
        headers={'User-Agent':'gdal4k-root-approved-artifact-materializer','Accept':'application/vnd.github+json' if api else 'application/octet-stream'}
        if api:headers.update({'Authorization':'Bearer '+self.token,'X-GitHub-Api-Version':'2022-11-28'})
        request=urllib.request.Request(url,headers=headers,method='GET')
        try:return self.opener.open(request,timeout=30)
        except urllib.error.HTTPError as error:return error
        except Exception:raise MaterializationError('HTTP_TRANSPORT_FAILURE') from None
    def json(self,path):
        url=self.api_url(path); before=utc(); response=self.response(url,True)
        try:
            observation=http_summary(url,response,'GitHub-REST-metadata',True)
            need(response.status==200,'API_METADATA_HTTP_STATUS')
            raw=response.read(2*M+1); need(len(raw)<=2*M,'API_METADATA_BOUND')
            value=parse(raw); observation.update(beforeUtc=before,afterUtc=utc(),bodyBytes=len(raw),bodySha256=hashlib.sha256(raw).hexdigest(),
                bodyRetention='Normalized identity retained separately; exact wire body hashed, not logged')
            return value,observation
        except MaterializationError as error:
            error.http=observation; raise
        except Exception:
            error=MaterializationError('API_METADATA_READ_FAILURE'); error.http=observation; raise error from None
        finally:response.close()
    def download_response(self,artifact_id,events):
        url=self.api_url('/actions/artifacts/'+str(artifact_id)+'/zip'); api=True
        for _ in range(4):
            response=self.response(url,api); events.append(http_summary(url,response,'official-artifact-download',api))
            if response.status==200:return response
            if response.status not in (301,302,303,307,308):
                response.close(); raise MaterializationError('ARTIFACT_HTTP_STATUS')
            location=response.headers.get('Location',''); response.close()
            need(qualified_location(location),'UNQUALIFIED_ARTIFACT_REDIRECT')
            url,api=location,False
        raise MaterializationError('ARTIFACT_REDIRECT_LIMIT')

def sealed_file_hash(fd):
    before=full(os.fstat(fd)); os.lseek(fd,0,os.SEEK_SET); digest=hashlib.sha256(); count=0
    while True:
        data=os.read(fd,CHUNK)
        if not data:break
        count+=len(data); need(count<=32*M,'SEALED_FILE_SIZE_BOUND'); digest.update(data)
    need(full(os.fstat(fd))==before,'SEALED_FILE_FULL11_CHANGED')
    return count,digest.hexdigest()

def partial_observation(fd,rawdir,rawroot,stage,final,observed):
    """Best-effort failure evidence; never replace the original download error."""
    result=dict(paths=[],pathObservations=[],bytes=None,sha256=None,full11=None,
                httpBodyBytesObserved=observed,notPublished=None,wholeRetainedFileSHA256Verified=False,
                bookkeepingErrors=[])
    def attempt(operation,call):
        try:return call()
        except Exception as error:
            result['bookkeepingErrors'].append(dict(operation=operation,errorType=type(error).__name__))
            return None
    attempt('partial-fsync-before-seal',lambda:os.fsync(fd))
    attempt('partial-chmod0400',lambda:os.fchmod(fd,0o400))
    attempt('partial-fsync-after-seal',lambda:os.fsync(fd))
    snapshot=attempt('partial-fstat',lambda:full(os.fstat(fd)))
    if snapshot is not None:
        result['full11']=snapshot; result['bytes']=snapshot['st_size']
    hashed=attempt('partial-whole-readback',lambda:sealed_file_hash(fd))
    if hashed is not None:
        result['bytes'],result['sha256']=hashed; result['wholeRetainedFileSHA256Verified']=True
    current=attempt('partial-final-fstat',lambda:full(os.fstat(fd)))
    if current is not None:result['full11']=current
    final_state='unknown'; final_matches=None
    for name in (stage,final):
        row=dict(path=rawroot+'/'+name,state='unknown',full11=None,pointsToDownloadFD=None)
        try:
            info=full(os.stat(name,dir_fd=rawdir,follow_symlinks=False));row.update(state='present',full11=info)
            if current is not None:row['pointsToDownloadFD']=(info['st_dev'],info['st_ino'])==(current['st_dev'],current['st_ino'])
            result['paths'].append(row['path'])
        except FileNotFoundError:row['state']='absent'
        except Exception as error:row['errorType']=type(error).__name__
        if name==final:final_state=row['state']; final_matches=row['pointsToDownloadFD']
        result['pathObservations'].append(row)
    result['notPublished']=True if final_state=='absent' or final_matches is False else False if final_matches is True else None
    return result

def primary_failure(error,artifact_id=None,pending=None):
    primary=error if isinstance(error,MaterializationError) else MaterializationError('UNEXPECTED_'+type(error).__name__)
    if artifact_id is not None:primary.artifact_id=artifact_id
    if pending is not None:primary.pending_download=pending
    return primary

def redacted_material(value):
    """Only typed OS identity fields; never transport data or exception strings."""
    if not isinstance(value,dict):return None
    result={}
    def safe_path(path):
        if isinstance(path,str) and len(path)<=1024 and re.fullmatch(r'/[A-Za-z0-9_./-]+',path):return path
        return '<path-redacted>'
    if 'path' in value:result['path']=safe_path(value['path'])
    if isinstance(value.get('paths'),list):result['paths']=[safe_path(p) for p in value['paths'][:2]]
    if type(value.get('bytes')) is int:result['bytes']=value['bytes']
    sha=value.get('sha256');result['sha256']=sha if isinstance(sha,str) and re.fullmatch('[0-9a-f]{64}',sha) else None
    info=value.get('full11')
    result['full11']={k:info[k] for k in FIELDS if type(info.get(k)) is int} if isinstance(info,dict) else None
    result['notPublished']=value.get('notPublished') if type(value.get('notPublished')) is bool else None
    if isinstance(value.get('lastKnownCompletedResult'),dict):
        nested=dict(value['lastKnownCompletedResult']);nested.pop('lastKnownCompletedResult',None)
        result['lastKnownCompletedResult']=redacted_material(nested)
    return result

def report_failure_and_raise(directory,path,value,primary,stage):
    """A broken metadata destination must not replace the original failure."""
    try:seal_json(directory,path,value)
    except Exception as report_error:
        try:
            # Only known OS identities, never signed URLs, headers or repr(error).
            pending=getattr(primary,'pending_download',None); code=primary.code
            valid_code=isinstance(code,str) and (re.fullmatch('[A-Z][A-Z0-9_]{0,95}',code) or
                       (code.startswith('UNEXPECTED_') and re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}',code[11:])))
            error_type=type(report_error).__name__
            fallback=dict(kind='GDAL-materializer-failure-stderr-fallback',primaryCode=code if valid_code else 'REDACTED_PRIMARY_CODE',
                reportingStage=stage,reportingErrorType=error_type if re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}',error_type) else 'Exception',
                artifactId=getattr(primary,'artifact_id',None),partial=redacted_material(primary.partial),
                pendingPublished=redacted_material(pending.get('materialization')) if isinstance(pending,dict) else None,
                metadataReceiptWritten=False,noAcceptanceClaim=True)
            raw=(json.dumps(fallback,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
            if len(raw)>8192:
                partial=fallback.get('partial') or {}; pending=fallback.get('pendingPublished') or {}
                fallback=dict(kind=fallback['kind'],primaryCode=fallback['primaryCode'],artifactId=fallback['artifactId'],
                    reportingStage=stage,partialPaths=partial.get('paths',[])[:2],pendingPath=pending.get('path'),
                    detailsTruncated=True,metadataReceiptWritten=False,noAcceptanceClaim=True)
                raw=(json.dumps(fallback,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
            if len(raw)<=8192:write_all(2,raw)
        except BaseException:
            # Even stderr/projection may fail. The original remains authoritative.
            pass
    raise primary from None

def publish_download_receipt(directory,path,receipt,artifact_id,result,pending):
    # Persist this in enclosing failure metadata even if the receipt write fails.
    pending['value']=dict(artifactId=artifact_id,materialization=result,receiptStatus='not-yet-sealed')
    return seal_json(directory,path,receipt)

def download_one(client,expected,rawdir,rawroot,events,tick=lambda _:None):
    identity=artifact_identity(expected); aid=identity['id']; final=str(aid)+'.zip'; stage=final+'.partial'
    # Both collision checks precede any HTTP request; neither path is overwritten.
    for name in (final,stage):
        try:os.stat(name,dir_fd=rawdir,follow_symlinks=False)
        except FileNotFoundError:continue
        raise MaterializationError('RAW_PATH_ALREADY_EXISTS')
    response=client.download_response(aid,events); fd=None; written=observed=0; incoming=hashlib.sha256(); start=time.monotonic(); failure=None; completed=None
    try:
        length=response.headers.get('Content-Length')
        need(length is None or (re.fullmatch('[0-9]{1,12}',length) is not None and int(length)==identity['bytes']),'CONTENT_LENGTH_DIFFERS')
        fd=os.open(stage,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_NOATIME,0o600,dir_fd=rawdir)
        while True:
            need(time.monotonic()-start<=300,'ARTIFACT_WALL_DEADLINE')
            data=response.read(min(CHUNK,identity['bytes']-written+1))
            if not data:break
            observed+=len(data); incoming.update(data)
            need(written+len(data)<=identity['bytes'],'ARTIFACT_SIZE_EXCEEDED')
            write_all(fd,data); written+=len(data); tick(written)
        need(written==identity['bytes'],'ARTIFACT_TRUNCATED')
        need(incoming.hexdigest()==identity['digest'][7:],'ARTIFACT_DIGEST_DIFFERS')
        os.fsync(fd); os.fchmod(fd,0o400); os.fsync(fd)
        actual_size,actual_sha=sealed_file_hash(fd)
        need((actual_size,actual_sha)==(identity['bytes'],identity['digest'][7:]),'SEALED_FILE_DIGEST_DIFFERS')
        prior=full(os.fstat(fd)); need(prior==full(os.stat(stage,dir_fd=rawdir,follow_symlinks=False)) and prior['st_nlink']==1,'PARTIAL_IDENTITY_CHANGED')
        os.link(stage,final,src_dir_fd=rawdir,dst_dir_fd=rawdir,follow_symlinks=False)
        need(os.fstat(fd).st_nlink==2,'PUBLICATION_LINK_COUNT')
        os.unlink(stage,dir_fd=rawdir); os.fsync(rawdir)
        sealed=full(os.fstat(fd)); need(sealed==full(os.stat(final,dir_fd=rawdir,follow_symlinks=False)) and
            sealed['st_nlink']==1 and sealed['st_uid']==os.getuid() and stat.S_IMODE(sealed['st_mode'])==0o400,'FINAL_FILE_SEAL')
        completed=dict(path=rawroot+'/'+final,bytes=actual_size,sha256=actual_sha,full11=sealed,
                    httpBodyBytesObserved=observed,httpBodySha256=incoming.hexdigest(),
                    sealedFileWholeSHA256Verified=True,elapsedSeconds=time.monotonic()-start)
        return completed
    except Exception as error:
        code=error.code if isinstance(error,MaterializationError) else 'DOWNLOAD_IO_FAILURE'
        partial=partial_observation(fd,rawdir,rawroot,stage,final,observed) if fd is not None else None
        failure=MaterializationError(code,partial)
        raise failure from None
    finally:
        close_errors=[]
        for operation,call in [('response-close',response.close),('file-close',lambda:os.close(fd) if fd is not None else None)]:
            try:call()
            except Exception as error:close_errors.append(dict(operation=operation,errorType=type(error).__name__))
        if close_errors:
            if failure is not None:
                if failure.partial is None:failure.partial=dict(paths=[],full11=None,bookkeepingErrors=[])
                failure.partial['closeErrors']=close_errors
            else:
                raise MaterializationError('DOWNLOAD_CLOSE_FAILURE',dict(lastKnownCompletedResult=completed,closeErrors=close_errors)) from None

def resource_sample(rawroot,outputroot,workspace,remaining):
    def text(path):
        with open(path,encoding='ascii') as source:value=source.read(262145)
        need(len(value)<=262144,'RESOURCE_METADATA_BOUND'); return value
    mounts=[]
    for line in text('/proc/self/mountinfo').splitlines():
        fields=line.split(); separator=fields.index('-')
        path=re.sub(r'\\([0-7]{3})',lambda m:chr(int(m[1],8)),fields[4]); mounts.append((path,fields[separator+1]))
    matching=[m for m in mounts if rawroot==m[0] or rawroot.startswith(m[0].rstrip('/')+'/')]
    need(matching,'RAW_MOUNT_UNKNOWN'); fs=max(matching,key=lambda m:len(m[0]))[1]
    need(fs not in ('tmpfs','ramfs','devtmpfs','hugetlbfs'),'RAW_MUST_BE_DISK_BACKED')
    groups={}
    for path,floor,charge in [(rawroot,2*1024**3,remaining),(outputroot,32*M,8*M),(workspace,32*M,0),('/tmp',2*1024**3,0),('/dev/shm',2*1024**3,0)]:
        device=os.stat(path).st_dev; v=os.statvfs(path); row=groups.setdefault(device,dict(paths=[],floor=0,charge=0,free=v.f_bavail*v.f_frsize))
        row['paths'].append(path); row['floor']=max(row['floor'],floor); row['charge']+=charge; row['free']=min(row['free'],v.f_bavail*v.f_frsize)
    for row in groups.values():need(row['free']>=row['floor']+row['charge'],'FILESYSTEM_FLOOR_AND_DESTINATION_CHARGE')
    memory={l.split(':')[0]:int(l.split()[1])*1024 for l in text('/proc/meminfo').splitlines()}
    stats={l.split()[0]:int(l.split()[1]) for l in text('/sys/fs/cgroup/memory.stat').splitlines()}
    maximum=text('/sys/fs/cgroup/memory.max').strip(); need(maximum!='max','FINITE_CGROUP_LIMIT_REQUIRED')
    current=int(text('/sys/fs/cgroup/memory.current')); safe=int(maximum)-current+stats['inactive_file']-128*M
    need(min(memory['MemAvailable'],safe)>=2*1024**3+648*M,'HOST_CGROUP_FLOOR_AND_RECEIVER_RESERVATION')
    peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024; need(peak<=128*M,'RSS128MIB')
    return dict(atUtc=utc(),filesystemGroups=list(groups.values()),rawFilesystemType=fs,
                hostAvailableBytes=memory['MemAvailable'],cgroupMaxBytes=int(maximum),cgroupCurrentBytes=current,
                cgroupInactiveFileBytes=stats['inactive_file'],cgroupSafeBytes=safe,cacheReserveBytes=128*M,
                receiverReservationBytes=648*M,processPeakRssBytes=peak,extraFileOrSlabCredit=False)

def validate_inputs(intent,cat,activation,qualified,worker,refs,now):
    need(set(activation)==ACTIVATION_KEYS and type(activation['schemaVersion']) is int and activation['schemaVersion']==1 and activation['kind']=='GDAL-remote-audit-prepare-activation' and activation['rootApprovedPrepare'] is True,'ACTIVATION_SCHEMA')
    scope=intent['scope']; need(activation['cohortManifest']==refs['intent'] and activation['sourceCatalogue']==refs['catalogue'],'ACTIVATION_PIN_BINDING')
    for key in ('repository','head','tree','base'):need(activation[key]==scope[key] and qualified[key]==scope[key],'ACTIVATION_COHORT_CONTEXT')
    need(type(cat['schemaVersion']) is int and cat['schemaVersion']==1 and cat['kind']=='GDAL-cohort-official-artifact-catalogue' and cat['cohortManifest']==refs['intent'] and
         cat['head']==scope['head'] and cat['repository']==scope['repository'],'CATALOGUE_CONTEXT')
    need(type(qualified['schemaVersion']) is int and qualified['schemaVersion']==1 and qualified['kind']=='GDAL-cohort-qualified-current-scope' and qualified['cohortManifest']==refs['intent'] and
         qualified['catalogue']==refs['catalogue'] and qualified['baseTree']==scope['baseTree'] and
         qualified['testCheckout']==scope['testCheckout'] and qualified['checkoutTree']==scope['tree'] and
         qualified['checkoutParents']==[scope['base'],scope['head']],'QUALIFIED_SCOPE_CONTEXT')
    begin=timestamp(qualified['observedStartedAtUtc']); end=timestamp(qualified['observedCompletedAtUtc']); at=timestamp(qualified['qualifiedAtUtc']); expiry=timestamp(qualified['expiresAtUtc'])
    need(qualified['maximumAgeSeconds']==intent['ci']['maximumAgeSeconds']==600 and begin<=end<=at<=now<expiry and expiry==begin+dt.timedelta(seconds=600),'FRESH_600S_SCOPE_REQUIRED_AT_START')
    runs={r['id']:r for r in cat['runs']}; need(len(runs)==len(cat['runs'])==2 and set(runs)=={scope['primaryRun'],scope['profileRun']} and sorted(cat['runs'],key=lambda r:r['id'])==sorted(qualified['runs'],key=lambda r:r['id']),'CATALOGUE_RUN_ATTEMPTS')
    for r in runs.values():need(type(r['attempt']) is int and r['attempt']>0 and r['status']=='completed' and r['conclusion']=='success' and r['head']==scope['head'],'CATALOGUE_RUN_SUCCESS')
    roles=qualified['requiredRoleChecks']; wanted={(scope[name+'Run'],role) for name in ('primary','profile') for role in intent['ci']['requiredRoles'][name]}
    need(len(roles)==len(wanted)==25 and {(r['runId'],r['name']) for r in roles}==wanted and len({r['id'] for r in roles})==25,'EXACT25_ROLE_IDENTITIES')
    for r in roles:need(type(r['id']) is int and r['id']>0 and r['attempt']==runs[r['runId']]['attempt'] and r['status']=='completed' and r['conclusion']=='success' and r['head']==scope['head'],'EXACT25_ROLE_SUCCESS')
    official={r['id']:r for r in cat['selectedArtifacts']}; need(len(official)==len(cat['selectedArtifacts'])>0,'SELECTED_ARTIFACT_IDS')
    normalized=[dict(id=x['id'],name=x['name'],size=x['bytes'],digest=x['digest'],runId=x['runId'],head=x['head'],expired=False) for x in map(artifact_identity,official.values())]
    need(sorted(normalized,key=lambda r:r['id'])==sorted(qualified['artifacts'],key=lambda r:r['id']),'QUALIFIED_SELECTED_IDENTITIES')
    for r in official.values():need(r['workflow_run']['id'] in runs and r['workflow_run']['head_sha']==scope['head'],'SELECTED_RUN_HEAD')
    families=cat['logicalFamilies']; covered=[]
    need(len(families)==len(intent['familyNames']) and sorted(f['original']['name'] for f in families)==intent['familyNames'],'LOGICAL_FAMILY_NAMES')
    for family in families:
        need(family['route'] in ('original-service-zip','audit-repack') and type(family['selected']) is list and family['selected'] and all(type(i) is int and i in official for i in family['selected']),'LOGICAL_FAMILY_ROUTE')
        covered.extend(family['selected'])
    need(sorted(covered)==sorted(official),'LOGICAL_FAMILY_SELECTED_PARTITION')
    need(type(worker['schemaVersion']) is int and worker['schemaVersion']==1 and worker['kind']=='GDAL-remote-audit-worker-context' and set(worker)=={'schemaVersion','kind','site','jobKey','workflowSHA','jobObservation'},'WORKER_CONTEXT_SCHEMA')
    site=worker['site']; need(set(site)=={'repository','head','runId','attempt','jobId','uid'} and site['repository']==scope['repository'] and site['head']==scope['head'] and type(site['uid']) is int and site['uid']==os.getuid(),'WORKER_SITE')
    for key,target in [('runId','auditRunId'),('attempt','auditRunAttempt'),('jobId','auditJobId')]:need(type(site[key]) is int and site[key]>0 and site[key]==activation[target],'WORKER_AUDIT_IDENTITIES')
    for env,value in [('GITHUB_REPOSITORY',site['repository']),('GITHUB_RUN_ID',str(site['runId'])),('GITHUB_RUN_ATTEMPT',str(site['attempt'])),('GITHUB_JOB',worker['jobKey']),('GITHUB_SHA',worker['workflowSHA'])]:need(os.environ.get(env)==value,'WORKER_ENV_CONTEXT')
    return scope,runs,official

def verify_job_body(raw,body):
    actual=json.dumps(parse(raw),sort_keys=True,separators=(',',':'),allow_nan=False)
    recorded=json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False)
    need(actual==recorded,'WORKER_JOB_RAW_BODY_BINDING')

def verify_job_identity(job,site):
    need(all(type(job.get(k)) is int for k in ('id','run_id','run_attempt')) and
         job['id']==site['jobId'] and job['run_id']==site['runId'] and job['run_attempt']==site['attempt'] and
         job['head_sha']==site['head'],'WORKER_JOB_REST_IDENTITY')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for option in ('intent','catalogue','activation','worker-context'):parser.add_argument('--'+option,nargs=2,metavar=('PATH','SHA256'),required=True)
    parser.add_argument('--output-root',required=True); parser.add_argument('--raw-root',required=True)
    args=parser.parse_args(); resource.setrlimit(resource.RLIMIT_AS,(128*M,128*M))
    refs={name:cli_ref(getattr(args,name.replace('-','_'))) for name in ('intent','catalogue','activation','worker-context')}
    values={}; baselines={}
    for name,r in refs.items():body,baselines[name]=read_pin(r); values[name]=parse(body)
    activation=values['activation']; extra={'qualified':activation['qualifiedScope'],'materializer':activation['materializerSource']}
    need(extra['materializer']['path']==os.path.abspath(__file__),'MATERIALIZER_SOURCE_PATH')
    for name,r in extra.items():body,baselines[name]=read_pin(r); refs[name]=r; values[name]=parse(body) if name=='qualified' else None
    scope,runs,official=validate_inputs(values['intent'],values['catalogue'],activation,values['qualified'],values['worker-context'],refs,dt.datetime.now(dt.timezone.utc))
    need(activation['rawRoot']==args.raw_root and activation['outputRoot']==args.output_root,'ROOT_DESTINATIONS')
    observation_ref=values['worker-context']['jobObservation']; observation_raw,observation_stat=read_pin(observation_ref); observation=parse(observation_raw)
    need(observation['method']=='GET' and observation['status']==200 and re.fullmatch('[0-9a-f]{64}',observation['bodySha256']) is not None,'WORKER_JOB_REST_OBSERVATION')
    job_raw,job_raw_stat=read_pin(observation['rawBody'])
    need(observation['bodySha256']==observation['rawBody']['sha256'],'WORKER_JOB_RAW_BODY_BINDING'); verify_job_body(job_raw,observation['body'])
    site=values['worker-context']['site']; joburl=urllib.parse.urlsplit(observation['url'])
    expectedpath='/repos/'+site['repository']+'/actions/runs/'+str(site['runId'])+'/attempts/'+str(site['attempt'])+'/jobs'
    need(joburl.scheme=='https' and joburl.netloc=='api.github.com' and joburl.path==expectedpath and not joburl.fragment and set(urllib.parse.parse_qs(joburl.query))<= {'page','per_page'},'WORKER_JOB_REST_URL')
    jobs=[j for j in observation['body']['jobs'] if j['id']==site['jobId']]
    need(len(jobs)==1,'WORKER_JOB_REST_IDENTITY'); verify_job_identity(jobs[0],site)
    need(timestamp(observation['beforeUtc'])<=timestamp(observation['afterUtc'])<=dt.datetime.now(dt.timezone.utc),'WORKER_JOB_OBSERVATION_TIME')
    token=os.environ.get('GITHUB_TOKEN'); client=RestClient(scope['repository'],token)
    output=make_directory(args.output_root); rawdir=None; receipts=[]; artifacts={}; samples=[]; run_observations=[]; pending_download={'value':None}
    started=utc(); remaining=sum(r['size_in_bytes'] for r in official.values())
    try:
        rawdir=make_directory(args.raw_root); samples.append(resource_sample(args.raw_root,args.output_root,values['intent']['source']['root'],remaining))
        invocation=seal_json(output,args.output_root+'/invocation.json',dict(kind='GDAL-GitHub-REST-materializer-invocation',startedAtUtc=started,pid=os.getpid(),uid=os.getuid(),argv=sys.argv,
            inputs=refs,workerContext=refs['worker-context'],activation=refs['activation'],jobObservation=observation_ref,qualifiedScopeCheckedFreshAtStart=True,
            httpAuthorizationPolicy='Bearer only to canonical api.github.com; never included in redirects or outputs',noMCPToolObservationsClaimed=True))
        for run_id,run in sorted(runs.items()):
            detail,http=client.json('/actions/runs/'+str(run_id)+'/attempts/'+str(run['attempt']))
            need(detail['id']==run_id and detail['run_attempt']==run['attempt'] and detail['head_sha']==scope['head'] and detail['head_branch']==scope['branch'] and
                 detail['status']=='completed' and detail['conclusion']=='success' and detail['repository']['id']==scope['repositoryId'] and detail['head_repository']['id']==scope['repositoryId'],'CURRENT_RUN_ATTEMPT_CHANGED')
            run_observations.append(dict(identity=run,http=http))
        for aid,expected in sorted(official.items()):
            events=[]; before=utc(); last=[0.0]
            try:
                detail,http=client.json('/actions/artifacts/'+str(aid)); verify_artifact(detail,expected,scope)
                samples.append(resource_sample(args.raw_root,args.output_root,values['intent']['source']['root'],remaining))
                def tick(written):
                    now=time.monotonic()
                    if now-last[0]>=0.5:
                        resource_sample(args.raw_root,args.output_root,values['intent']['source']['root'],remaining-written); last[0]=now
                result=download_one(client,expected,rawdir,args.raw_root,events,tick)
                receipt=dict(schemaVersion=1,kind='GDAL-GitHub-REST-artifact-materialization-receipt',status='completed',artifactId=aid,officialArtifact=artifact_identity(expected),
                    currentMetadata=http,beforeUtc=before,afterUtc=utc(),downloadHTTP=events,materialization=result,workerContext=refs['worker-context'],activation=refs['activation'],invocation=invocation,
                    provenance='Actual Python HTTPS requests and OS writes; no MCP/plugin completed-chain claim',credentialsOrSignedQueriesRetained=False,noFamilyAcceptanceClaim=True)
                rr=publish_download_receipt(output,args.output_root+'/'+str(aid)+'-receipt.json',receipt,aid,result,pending_download); receipts.append(rr)
                artifacts[str(aid)]={k:result[k] for k in ('path','bytes','sha256','full11')}; artifacts[str(aid)].update(artifactId=aid,receipt=rr,materialization='new-normal-service-download')
                remaining-=result['bytes']; pending_download['value']=None
            except Exception as error:
                primary=primary_failure(error,aid,pending_download['value'])
                failure=dict(schemaVersion=1,kind='GDAL-GitHub-REST-artifact-materialization-failure',artifactId=aid,beforeUtc=before,afterUtc=utc(),
                    errorCode=primary.code,partial=primary.partial,downloadHTTP=events,metadataHTTP=getattr(error,'http',None),
                    retainedCompletedReceipts=receipts,publishedCurrentReceiptPending=pending_download['value'],noRetryOrCleanupPerformed=True,credentialsOrSignedQueriesRetained=False,noFamilyAcceptanceClaim=True)
                report_failure_and_raise(output,args.output_root+'/'+str(aid)+'-failure.json',failure,primary,'artifact-failure-receipt')
        need(remaining==0 and len(artifacts)==len(official),'SELECTED_COMPLETENESS')
        for r in artifacts.values():need(full(os.lstat(r['path']))==r['full11'],'FINAL_RAW_FULL11_CHANGED')
        mapvalue=dict(schemaVersion=1,kind='GDAL-cohort-materialization-map',cohortManifest=refs['intent'],repository=scope['repository'],head=scope['head'],sourceCatalogue=refs['catalogue'],
            artifacts=artifacts,remotePreparationActivation=refs['activation'],workerContext=refs['worker-context'],atUtc=utc(),selectedArtifactCount=len(artifacts),
            selectedArtifactBytes=sum(r['bytes'] for r in artifacts.values()),allSelectedWholeOfficialSHAsFreshlyVerified=True,noFamilyAcceptanceClaim=True,
            provenance='Genuine GitHub REST download and sealed-file SHA receipts; same-runner OS full11')
        mapref=seal_json(output,args.output_root+'/materialization-map.json',mapvalue)
        samples.append(resource_sample(args.raw_root,args.output_root,values['intent']['source']['root'],0))
        for name,r in refs.items():need(read_pin(r)[1]==baselines[name],'INPUT_FULL11_CHANGED')
        need(read_pin(observation_ref)[1]==observation_stat and read_pin(observation['rawBody'])[1]==job_raw_stat,'WORKER_OBSERVATION_CHANGED')
        for r in artifacts.values():need(full(os.lstat(r['path']))==r['full11'],'POST_MAP_RAW_FULL11_CHANGED')
        prepared=dict(schemaVersion=1,kind='GDAL-remote-audit-PREPARED',status='PREPARED',startedAtUtc=started,completedAtUtc=utc(),context=values['worker-context']['site'],
            workerContext=refs['worker-context'],cohortManifest=refs['intent'],sourceCatalogue=refs['catalogue'],qualifiedScope=refs['qualified'],activation=refs['activation'],materializerSource=refs['materializer'],
            materializationMap=mapref,invocation=invocation,selectedArtifactCount=len(artifacts),selectedArtifactBytes=mapvalue['selectedArtifactBytes'],receipts=receipts,
            currentRunObservations=run_observations,resources=samples,rawFull11BeforeAndAfterMapExact=True,sourceInputsFull11Unchanged=True,
            source20GuardAuthority='Main worker snapshots unchanged predicate closure separately',sameRunnerRequired=True,noMCPCompletedChainClaim=True,
            runtimeAccepted=False,overallGoalComplete=False)
        result=seal_json(output,args.output_root+'/prepared.json',prepared)
        print(json.dumps({'prepared':result},sort_keys=True))
    except Exception as error:
        primary=primary_failure(error,pending=pending_download['value'])
        failure=dict(kind='GDAL-remote-audit-prepare-failure',errorCode=primary.code,
            httpObservation=getattr(error,'http',None),partial=primary.partial,currentRunObservations=run_observations,completedReceipts=receipts,publishedCurrentReceiptPending=pending_download['value'],startedAtUtc=started,stoppedAtUtc=utc(),noRetryOrCleanupPerformed=True,noAcceptanceClaim=True)
        report_failure_and_raise(output,args.output_root+'/prepare-failure.json',failure,primary,'prepare-failure-receipt')
    finally:
        try:
            for name,r in refs.items():need(read_pin(r)[1]==baselines[name],'INPUT_FULL11_CHANGED')
            for r in artifacts.values():need(full(os.lstat(r['path']))==r['full11'],'FINAL_RAW_FULL11_CHANGED')
            need(read_pin(observation_ref)[1]==observation_stat and read_pin(observation['rawBody'])[1]==job_raw_stat,'WORKER_OBSERVATION_CHANGED')
        finally:
            if rawdir is not None:os.close(rawdir)
            os.close(output)

if __name__=='__main__':
    try:main()
    except Exception as error:
        # Never render urllib exceptions, signed URLs, request headers or token values.
        print('remote_materializer failed: '+(error.code if isinstance(error,MaterializationError) else type(error).__name__),file=sys.stderr)
        raise SystemExit(1)

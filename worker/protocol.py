"""Small shared I/O rules for the separately reviewed remote executor."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

M=1048576
MAX_METADATA=8*M
PART_BYTES=24*M-65536
MAX_PROOF=256*M
STAT_KEYS=('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size',
           'st_atime_ns','st_mtime_ns','st_ctime_ns','st_blocks')

def need(ok,why):
    if not ok:raise ValueError(why)

def full11(s):return {key:getattr(s,key)for key in STAT_KEYS}

def canonical(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\n').encode()

def same(a,b):return canonical(a)==canonical(b)

def absolute(value):
    need(type(value)is str and value.startswith('/')and not value.startswith('//')and str(Path(value))==value and
         '..'not in Path(value).parts and '\\'not in value,'Canonical absolute path required')
    return Path(value)

def relative(value):
    need(type(value)is str and value and not value.startswith('/')and '\\'not in value and ':'not in value and
         str(Path(value))==value and all(x not in ('','.','..')for x in Path(value).parts),'Safe relative path required')
    return value

def integer(value,minimum=0,maximum=2**63-1):
    need(type(value)is int and minimum<=value<=maximum,'Exact bounded integer required')
    return value

def digest(value,length=64):
    need(type(value)is str and re.fullmatch('[0-9a-f]{'+str(length)+'}',value),'Digest differs')
    return value

def reference(value,maximum=MAX_METADATA):
    need(type(value)is dict and set(value)=={'path','bytes','sha256'},'Exact immutable reference required')
    absolute(value['path']);integer(value['bytes'],0,maximum);digest(value['sha256']);return value

def site(value):
    need(type(value)is dict and set(value)=={'repository','head','runId','attempt','jobId','uid'},'Exact execution site required')
    need(type(value['repository'])is str and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',value['repository']),'Repository differs')
    digest(value['head'],40)
    for key in ('runId','attempt','jobId'):integer(value[key],1,2**53-1)
    integer(value['uid'],0,2**32-1);return value

def toolkit(value):
    need(type(value)is dict and set(value)=={'commit','manifestSha256'},'Exact toolkit identity required')
    digest(value['commit'],40);digest(value['manifestSha256']);return value

def parents(path,create=False):
    path=absolute(str(path));fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            if create:
                try:os.mkdir(part,0o700,dir_fd=fd);os.fsync(fd)
                except FileExistsError:pass
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd
    except BaseException:os.close(fd);raise

def read_bytes(path,maximum=MAX_METADATA,sealed=True):
    path=absolute(str(path));parent=parents(path)
    try:
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(fd,'rb')as f:
            before=full11(os.fstat(f.fileno()))
            need(stat.S_ISREG(before['st_mode'])and before['st_nlink']==1 and before['st_uid']==os.getuid()and
                 before['st_size']<=maximum and(not sealed or stat.S_IMODE(before['st_mode'])==0o400),'Owned bounded regular file required')
            raw=f.read(maximum+1)
            need(len(raw)==before['st_size']and before==full11(os.fstat(f.fileno()))==
                 full11(os.stat(path.name,dir_fd=parent,follow_symlinks=False)),'NoTime file identity changed')
    finally:os.close(parent)
    return raw,before

def parse(raw):
    def pairs(items):
        value={}
        for key,item in items:need(key not in value,'Duplicate JSON key');value[key]=item
        return value
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda value:(_ for _ in ()).throw(ValueError(value)))

def read_pin(ref,json_value=True,maximum=MAX_METADATA):
    reference(ref,maximum);raw,_=read_bytes(ref['path'],maximum)
    need(len(raw)==ref['bytes']and hashlib.sha256(raw).hexdigest()==ref['sha256'],'Pinned bytes differ')
    return parse(raw)if json_value else raw

def pin(path,maximum=MAX_METADATA):
    raw,_=read_bytes(path,maximum);return {'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}

def create(path,raw,mode=0o400):
    path=absolute(str(path));parent=parents(path,True)
    try:
        fd=os.open(path.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode,dir_fd=parent)
        with os.fdopen(fd,'wb')as f:f.write(raw);f.flush();os.fsync(f.fileno())
        os.fsync(parent)
    finally:os.close(parent)
    return {'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}

def seal(path,value):
    raw=canonical(value);need(len(raw)<=MAX_METADATA,'Metadata record limit');return create(path,raw)

def runtime_resources():
    memory=Path('/proc/meminfo').read_text()
    available=next(int(line.split()[1])*1024 for line in memory.splitlines()if line.startswith('MemAvailable:'))
    relative=next(line.rstrip().split('::',1)[1]for line in Path('/proc/self/cgroup').read_text().splitlines()if line.startswith('0::'))
    root=Path('/sys/fs/cgroup');current=root/relative.lstrip('/');groups=[]
    while True:
        maximum=(current/'memory.max').read_text().strip();used=int((current/'memory.current').read_text())
        values={line.split()[0]:int(line.split()[1])for line in(current/'memory.stat').read_text().splitlines()}
        limit=None if maximum=='max'else int(maximum)
        groups.append({'path':str(current),'limitBytes':limit,'currentBytes':used,'inactiveFileBytes':values['inactive_file'],
            'safeHeadroomBytes':None if limit is None else max(0,limit-used+values['inactive_file']-128*M),
            'grossHeadroomBytes':None if limit is None else max(0,limit-used+values['inactive_file']),
            'cacheReserveBytes':128*M,'activeFileCredited':False})
        if current==root:break
        need(current.is_relative_to(root),'Cgroup hierarchy escaped');current=current.parent
    fs={}
    for name in ('/workspace','/tmp','/dev/shm'):
        fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:s=os.fstat(fd);v=os.fstatvfs(fd)
        finally:os.close(fd)
        fs[name]={'device':s.st_dev,'availableBytes':v.f_bavail*v.f_frsize}
    return {'hostMemAvailableBytes':available,'cgroups':groups,'filesystems':fs,
            'peakRssBytes':int(Path('/proc/self/status').read_text().split('VmHWM:')[1].split()[0])*1024}

def admit_runtime(sample,*,memory_extra=424*M,output=None,output_reservation=0):
    need(sample['peakRssBytes']<=128*M,'Worker RSS128MiB bound')
    need(sample['hostMemAvailableBytes']>=2*1024**3+memory_extra,'Host actual reservation does not fit')
    finite=[g for g in sample['cgroups']if g['limitBytes']is not None]
    need(finite,'No observable finite cgroup; no max waiver')
    for group in finite:
        need(group['activeFileCredited']is False and group['cacheReserveBytes']==128*M and
             group['safeHeadroomBytes']>=2*1024**3+memory_extra and
             group['grossHeadroomBytes']>=2*1024**3+memory_extra,'Cgroup actual inactive-only reservation does not fit')
    groups={}
    for path,value in sample['filesystems'].items():
        floor=32*M if path=='/workspace'else 2*1024**3
        g=groups.setdefault(value['device'],[value['availableBytes'],0,0])
        g[0]=min(g[0],value['availableBytes']);g[1]=max(g[1],floor)
        if path=='/dev/shm':g[2]+=8*M
    if output is not None:
        parent=absolute(str(output)).parent
        while not parent.exists():parent=parent.parent
        fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:s=os.fstat(fd);v=os.fstatvfs(fd)
        finally:os.close(fd)
        g=groups.setdefault(s.st_dev,[v.f_bavail*v.f_frsize,32*M,0]);g[0]=min(g[0],v.f_bavail*v.f_frsize);g[2]+=output_reservation
    for available,floor,reserved in groups.values():need(available>=floor+reserved,'Actual filesystem floor/reservations do not fit')

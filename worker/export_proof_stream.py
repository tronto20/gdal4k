"""Export regular metadata as a bounded indexed stream; never export raw ZIPs."""
import argparse
import hashlib
import os
from pathlib import Path
import resource
import stat
from protocol import (M,PART_BYTES,MAX_PROOF,MAX_METADATA,absolute,canonical,create,
                      digest,full11,integer,need,parents,parse,read_pin,seal,site,toolkit,
                      runtime_resources,admit_runtime)

def export(manifest,output):
    need(type(manifest)is dict and set(manifest)=={'site','toolkit','stage','roots','includeFiles','sourcePins','graph'},'Export input fields differ')
    site(manifest['site']);toolkit(manifest['toolkit'])
    need(type(manifest['stage'])is str and type(manifest['roots'])is list and type(manifest['includeFiles'])is list,'Export paths differ')
    need(type(manifest['sourcePins'])is list and len(manifest['sourcePins'])==20,'Exact source20 required')
    need(type(manifest['graph'])is dict and set(manifest['graph'])=={'prepared','commands','gates'},'Graph roots differ')
    output=absolute(str(output));need(not os.path.lexists(output),'Fresh proof export required')
    files={}
    def add(path):
        path=absolute(str(path));pfd=parents(path)
        try:s=os.stat(path.name,dir_fd=pfd,follow_symlinks=False)
        finally:os.close(pfd)
        need(stat.S_ISREG(s.st_mode)and s.st_uid==os.getuid()and s.st_nlink==1 and
             stat.S_IMODE(s.st_mode)in(0o400,0o500),'Only owned sealed regular metadata may be exported')
        need(not path.name.endswith('.zip')and s.st_size<=96*M,'Raw/archive export forbidden or file exceeds bound')
        files[str(path)]=full11(s)
    def walk(root):
        root=absolute(str(root));fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NOATIME)
        try:
            with os.scandir(fd)as entries:rows=[(x.name,x.stat(follow_symlinks=False))for x in entries]
        finally:os.close(fd)
        for name,s in sorted(rows):
            need(s.st_uid==os.getuid()and not stat.S_ISLNK(s.st_mode),'Export tree owner/link differs')
            if stat.S_ISDIR(s.st_mode):walk(root/name)
            else:add(root/name)
    for root in manifest['roots']:walk(root)
    for path in manifest['includeFiles']:add(path)
    need(len(files)<=10000 and sum(x['st_size']for x in files.values())<=MAX_PROOF,'Proof stream256MiB bound')
    total=sum(x['st_size']for x in files.values())
    # Reserve the bounded2MiB index plus directory/block-rounding headroom;
    # payload-only reservation would cross a physical floor at equality.
    index_reserve=4*M
    admit_runtime(runtime_resources(),output=output,output_reservation=total+index_reserve)
    pfd=parents(output,True)
    try:os.mkdir(output.name,0o700,dir_fd=pfd);os.fsync(pfd)
    finally:os.close(pfd)
    parts=[];inventory=[];stream=hashlib.sha256();offset=0;current=None;part_hash=None;part_size=0;part_start=0
    def finish():
        nonlocal current,part_hash,part_size
        if current is None:return
        current.flush();os.fsync(current.fileno());os.fchmod(current.fileno(),0o400);current.close()
        parts.append({'name':'part-%04d.bin'%(len(parts)+1),'offset':part_start,'bytes':part_size,'sha256':part_hash.hexdigest()})
        current=None
    try:
        for path,before in sorted(files.items()):
            start=offset;whole=hashlib.sha256();count=0;pfd=parents(path)
            try:
                fd=os.open(Path(path).name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME,dir_fd=pfd)
                with os.fdopen(fd,'rb')as source:
                    need(full11(os.fstat(source.fileno()))==before,'Export input changed before read')
                    while True:
                        data=source.read(65536)
                        if not data:break
                        count+=len(data);need(count<=before['st_size'],'Export input grew');whole.update(data)
                        rest=memoryview(data)
                        while rest:
                            if current is None:
                                part_start=offset;part_size=0;part_hash=hashlib.sha256()
                                name='part-%04d.bin'%(len(parts)+1);fd2=os.open(output/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                                current=os.fdopen(fd2,'wb')
                            take=min(len(rest),PART_BYTES-part_size);block=rest[:take]
                            current.write(block);stream.update(block);part_hash.update(block);part_size+=take;offset+=take;rest=rest[take:]
                            if part_size==PART_BYTES:
                                finish();admit_runtime(runtime_resources(),output=output,output_reservation=total-offset+index_reserve)
                    need(count==before['st_size']and before==full11(os.fstat(source.fileno()))==
                         full11(os.stat(Path(path).name,dir_fd=pfd,follow_symlinks=False)),'Export input full11 changed')
            finally:os.close(pfd)
            inventory.append({'remotePath':path,'logicalPath':path.lstrip('/'),'offset':start,'bytes':count,
                              'sha256':whole.hexdigest(),'full11':before})
        finish()
    finally:
        if current is not None:current.close()
    index={'schemaVersion':1,'kind':'GDAL-remote-audit-proof-index','site':manifest['site'],'toolkit':manifest['toolkit'],
           'stage':manifest['stage'],'streamBytes':offset,'streamSha256':stream.hexdigest(),'parts':parts,
           'files':inventory,'sourcePins':manifest['sourcePins'],'graph':manifest['graph']}
    need(len(canonical(index))<=2*M,'Proof index2MiB limit')
    result=seal(output/'proof-index.json',index)
    directory=os.open(output,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);os.fsync(directory);os.close(directory)
    admit_runtime(runtime_resources())
    return result

def main():
    resource.setrlimit(resource.RLIMIT_AS,(128*M,128*M))
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',nargs=2,required=True);p.add_argument('--output',required=True)
    a=p.parse_args();path=absolute(a.manifest[0]);ref={'path':str(path),'bytes':os.lstat(path).st_size,'sha256':digest(a.manifest[1])}
    print(__import__('json').dumps(export(read_pin(ref),a.output)))

if __name__=='__main__':main()

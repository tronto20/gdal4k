"""Bounded current Maven forward TAR header census, before nested ZIP audits.

The traversal reuses the reviewed family/index/transport readers. It drains
ordinary bodies in bounded chunks and never materializes nested ZIP bodies.
Capacity forecasts are prospective; this does not grant family acceptance.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import zipfile
import cohort_policy_v1 as policy

MiB=1048576
MAX_METADATA=2*MiB
CHUNK=65536


class CensusError(ValueError):pass


def need(value,message):
    if not value:raise CensusError(message)


def load_family():
    policy.initialize_for_process()
    import c23_family_source_v1 as family
    return family


def allocation_charge(size,unit):
    need(type(size) is int and size>=0 and type(unit) is int and unit>0,'Invalid prospective allocation charge')
    return ((size+unit-1)//unit)*unit


def census_tar_headers(source,codec,*,chain,audit,expected_bytes=None,expected_sha256=None,
                       part_scratch_charge_bytes=0,metadata_budget_bytes=MAX_METADATA):
    """Observe fully authenticated forward headers without opening nested ZIPs."""
    family=load_family();core=family.core
    need(codec in ('gzip','xz') and type(chain) is list and chain and all(type(v) is str for v in chain),'Invalid census stream identity')
    need(type(part_scratch_charge_bytes) is int and part_scratch_charge_bytes>=0 and
         type(metadata_budget_bytes) is int and 0<metadata_budget_bytes<=MAX_METADATA,'Invalid prospective census metadata/scratch policy')
    if expected_bytes is not None:need(type(expected_bytes) is int and expected_bytes>=0,'Invalid expected compressed length')
    if expected_sha256 is not None:need(type(expected_sha256) is str and len(expected_sha256)==64,'Invalid expected compressed digest')
    unit=os.statvfs(audit.scratch).f_frsize
    decoder=core.transport._Decoder(source,codec,memory_limit=audit.config.xz_memory_limit,
                                    max_decoded_bytes=audit.config.max_decoded_bytes)
    buffered=io.BufferedReader(core.transport._ChunkReader(decoder.chunks()),buffer_size=CHUNK)
    count,last_end,member_bytes,metadata_bytes=0,0,0,0
    members=[];violations=[];seen=set()
    try:
        with tarfile.open(fileobj=buffered,mode='r|',tarinfo=core.transport._LimitedTarInfo) as archive:
            for member in archive:
                directory=member.type==tarfile.DIRTYPE
                name=core.safe_name(member.name[:-1] if directory and member.name.endswith('/') else member.name)
                need(member.type in (tarfile.REGTYPE,tarfile.AREGTYPE,tarfile.DIRTYPE) and member.sparse is None and
                     (not directory or member.size==0),'Unsupported TAR header in census')
                need(name.casefold() not in seen,'Duplicate TAR path in census');seen.add(name.casefold())
                count+=1;need(count<=audit.config.max_entries,'Header census member count limit exceeded')
                need(type(member.size) is int and 0<=member.size<=audit.config.max_decoded_bytes,'Invalid TAR declared member length')
                member_bytes+=member.size;need(member_bytes<=audit.config.max_decoded_bytes,'TAR member byte census limit exceeded')
                if not directory and name.lower().endswith(('.jar','.aar','.klib','.zip')):
                    nested_charge=allocation_charge(member.size,unit)
                    prospective=part_scratch_charge_bytes+nested_charge
                    entry={'chain':[*chain,name],'declaredStoredArchiveBytes':member.size,
                        'prospectiveNestedAllocationBytes':nested_charge,
                        'prospectiveRawPartAllocationBytes':part_scratch_charge_bytes,
                        'prospectiveJointAllocationBytes':prospective,
                        'fitsDeclaredNestedLimit':member.size<=audit.config.max_nested_bytes,
                        'fitsDeclaredGlobalScratchLimit':prospective<=audit.config.max_scratch_bytes,
                        'nestedBodyMaterialized':False}
                    charge=len(json.dumps(entry,ensure_ascii=False,indent=2).encode())+256
                    need(metadata_bytes+charge<=metadata_budget_bytes,'Header census metadata limit exceeded')
                    metadata_bytes+=charge;members.append(entry)
                    if not entry['fitsDeclaredNestedLimit'] or not entry['fitsDeclaredGlobalScratchLimit']:
                        violations.append(entry)
                # Reading and discarding is deliberate. Even an oversized nested
                # header must reach EOF before its compressed identity is trusted.
                if not directory:
                    copied=0
                    with archive.extractfile(member) as body:
                        while data:=body.read(CHUNK):
                            copied+=len(data);need(copied<=member.size,'TAR body exceeds declared header length')
                            audit._checkpoint()
                    need(copied==member.size,'TAR body is truncated during census')
                last_end=member.offset_data+((member.size+511)//512)*512
                archive.members.clear();audit._checkpoint()
            padding=archive.fileobj.tell()-last_end
            while data:=archive.fileobj.read(CHUNK):
                need(not data.strip(b'\0'),'Nonzero trailing TAR data in census');padding+=len(data)
            need(padding>=1024,'Truncated TAR end markers in census')
        summary=decoder.summary()
        need(count>0,'Empty TAR census is outside the current staged contract')
        if expected_bytes is not None:need(summary['archiveBytes']==expected_bytes,'Actual compressed TAR length differs from authenticated index')
        if expected_sha256 is not None:need(summary['archiveSha256']==expected_sha256,'Actual compressed TAR SHA differs from authenticated index')
        audit._checkpoint(force=True)
        return {'headerCount':count,'ordinaryAndNestedDeclaredBytes':member_bytes,
            'compressedAndDecodedIdentity':summary,'storedNestedArchives':members,
            'capacityAdmitted':not violations,'capacityViolations':violations,
            'declaredNestedArchiveLimitBytes':audit.config.max_nested_bytes,
            'declaredGlobalScratchLimitBytes':audit.config.max_scratch_bytes,
            'prospectiveRawPartAllocationBytes':part_scratch_charge_bytes,
            'allocationUnitBytes':unit,'chargedCensusMetadataBytes':metadata_bytes,
            'recursiveNestedZIPPeakProven':False,'nestedZIPBodiesMaterialized':False,
            'fullCompressedStreamAndTARZeroEndVerified':True,'actualFamilyAcceptanceClaimed':False}
    except CensusError:raise
    except (tarfile.TarError,core.transport.ArchiveError,core.AuditError,EOFError,OSError,ValueError) as error:
        raise CensusError(str(error)) from error
    finally:buffered.close()


def read_reference(reference,*,json_value=True,sealed=True):
    family=load_family();core=family.core
    policy.validate_reference(reference,MAX_METADATA)
    with core.open_readonly(reference['path']) as source:
        before=core.full11(os.fstat(source.fileno()))
        need(before['st_size']==reference['bytes'],'Pinned census metadata length differs')
        if sealed:need(before['st_uid']==os.getuid() and before['st_nlink']==1 and stat.S_IMODE(before['st_mode'])==0o400,
                       'Census metadata/source must be owned0400/nlink1 before actual invocation')
        raw=source.read(reference['bytes']+1)
        need(len(raw)==reference['bytes'] and hashlib.sha256(raw).hexdigest()==reference['sha256'],'Pinned census metadata whole SHA differs')
        need(core.full11(os.fstat(source.fileno()))==before and core.full11(os.lstat(reference['path']))==before,'Pinned census metadata full11 changed')
    return family.parse_json(raw) if json_value else raw


def source_reference(path):
    family=load_family();core=family.core
    with core.open_readonly(path) as source:
        before=core.full11(os.fstat(source.fileno()));need(before['st_size']<=MAX_METADATA,'Census source/metadata exceeds2MiB')
        raw=source.read(MAX_METADATA+1)
        need(len(raw)==before['st_size'] and core.full11(os.fstat(source.fileno()))==before and
             core.full11(os.lstat(path))==before,'Census source/metadata changed during NoTime pin')
    return {'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}


def required_source_paths():
    family=load_family()
    return {str(Path(path).resolve()) for path in [__file__,family.__file__,family.core.__file__,policy.__file__,
        family.core.strict_zip.__file__,family.core.transport.__file__,family.collector.__file__]}


def maven_family_names(intent):
    names=[name for name in intent['familyNames'] if name in ('gdal4k-jvm-maven','gdal4k-maven') or
        name.startswith('profile-') and not name.startswith(('profile-evidence-','profile-cross-evidence-'))]
    need(len(names)==intent['packages']['profileFamilies']+2 and set(('gdal4k-jvm-maven','gdal4k-maven'))<=set(names),
         'Current source-derived Maven family matrix differs')
    return sorted(names,key=lambda name:(name!='gdal4k-jvm-maven',name))


def prospective_limits(intent):
    resources=intent['resources']
    return {name:resources[name] for name in ('nestedArchiveLimitBytes','scratchLimitBytes','rssLimitBytes','captureFileLimitBytes')}


def verify_source_approval(reference):
    approval=read_reference(reference)
    required={'schemaVersion','kind','cohortManifest','approvedForActualHeaderCensus','sources'}
    need(type(approval) is dict and required<=set(approval) and set(approval)<=required|{'reviewReferences','atUtc'} and
         type(approval['schemaVersion']) is int and approval['schemaVersion']==1 and
         approval.get('kind')=='GDAL-cohort-header-census-source-approval' and
         approval.get('approvedForActualHeaderCensus') is True,'Dedicated actual header census source approval absent')
    policy.require_intent_reference(approval.get('cohortManifest'))
    need(type(approval.get('sources')) is list and len(approval['sources'])==7,
         'Census approval source closure is absent or excessive')
    need(type(approval.get('reviewReferences',[])) is list and len(approval.get('reviewReferences',[]))<=64,
         'Census source review reference inventory differs')
    refs={}
    for ref in approval['sources']:
        policy.validate_reference(ref,MAX_METADATA)
        need(ref['path'] not in refs,'Duplicate census source approval path')
        read_reference(ref,json_value=False);refs[ref['path']]=ref
    need(set(refs)==required_source_paths(),'Census approval must contain exactly its seven actual imported sources')
    for reference in approval.get('reviewReferences',[]):read_reference(reference,json_value=False)
    return approval


def bound_inputs(catalogue_ref,map_ref):
    """Validate metadata seals and typed source references before raw handles exist."""
    family=load_family();catalogue=read_reference(catalogue_ref);materialization=read_reference(map_ref)
    policy.require_intent_reference(catalogue.get('cohortManifest'))
    policy.require_intent_reference(materialization.get('cohortManifest'))
    policy.validate_reference(materialization.get('sourceCatalogue'),MAX_METADATA)
    need(materialization['sourceCatalogue']==catalogue_ref,'Materialization source catalogue differs')
    return family.FamilyInputs(catalogue_ref,map_ref)


def compile_plan(catalogue_ref,map_ref,approval_ref,scratch_root):
    """Compile concrete pinned metadata inputs; does not open raw artifact bodies."""
    family=load_family();intent=policy.current()
    verify_source_approval(approval_ref)
    inputs=bound_inputs(catalogue_ref,map_ref)
    by_name={row['original']['name']:row['original']['id'] for row in inputs.families.values()}
    return {'schemaVersion':1,'kind':'GDAL-cohort-header-census-plan',
        **{key:intent['scope'][key] for key in ('head','tree','base')},'cohortManifest':policy.current_ref(),
        'catalogue':catalogue_ref,'materializationMap':map_ref,'mavenFamilyIds':[by_name[name] for name in maven_family_names(intent)],
        'scratchRoot':str(scratch_root),'sourceApproval':approval_ref,'prospectiveLimits':prospective_limits(intent),
        'censusSource':source_reference(Path(__file__).resolve())}


def validate_plan(plan):
    intent=policy.current()
    need(type(plan) is dict and set(plan)=={'schemaVersion','kind','head','tree','base','cohortManifest','catalogue',
        'materializationMap','mavenFamilyIds','scratchRoot','sourceApproval','prospectiveLimits','censusSource'},'Unexpected census plan schema')
    need(type(plan['schemaVersion']) is int and plan['schemaVersion']==1 and plan['kind']=='GDAL-cohort-header-census-plan' and
         all(plan[key]==intent['scope'][key] for key in ('head','tree','base')),'Census plan source H/T/B differs')
    policy.require_plan_intent(plan)
    for key in ('catalogue','materializationMap','sourceApproval','censusSource'):policy.validate_reference(plan[key],MAX_METADATA)
    need(type(plan['mavenFamilyIds']) is list and all(type(value) is int and value>0 for value in plan['mavenFamilyIds']) and
         len(set(plan['mavenFamilyIds']))==len(plan['mavenFamilyIds']),'Census family ID sequence differs')
    need(type(plan['prospectiveLimits']) is dict and set(plan['prospectiveLimits'])==set(prospective_limits(intent)) and
         all(type(value) is int for value in plan['prospectiveLimits'].values()) and
         plan['prospectiveLimits']==prospective_limits(intent),'Census cannot change prospectively declared limits')
    root=Path(plan['scratchRoot']);need(root.is_absolute() and str(root)==plan['scratchRoot'] and '..' not in root.parts and
         root.resolve()==root,'Census scratch root must be absolute/canonical without symlinks')
    need(plan['censusSource']==source_reference(Path(__file__).resolve()),'Census plan does not pin this exact source')
    return plan


@contextmanager
def outer_source(inputs,row,audit):
    family=load_family();original=row['original'];chain=[original['name']+'.zip']
    if row['route']=='original-service-zip':
        need(row['selected']==[original['id']],'Direct census route does not preserve original service ZIP')
        with inputs.raw(original['id']) as source:yield source,None,0,chain
    else:
        need(row['route']=='audit-repack','Unsupported current census source route')
        indices=[aid for aid in row['selected'] if inputs.official[aid]['name'].endswith('--index')]
        need(len(indices)==1,'Current Maven family lacks one index route')
        values,_=family.bounded_index_members(inputs,indices[0],audit)
        manifest,index,_=family.validate_index(inputs,row,values)
        unit=os.statvfs(audit.scratch).f_frsize
        part_charge=max((allocation_charge(part['bytes'],unit) for part in manifest['parts']),default=0)
        chain=[original['name']+'.audit-repack.zip']
        with family.VirtualParts(inputs,manifest,index,audit,chain) as virtual:
            yield virtual,{entry['path']:entry for entry in manifest['zipMembers']},part_charge,chain


def census_family(inputs,row,audit,metadata_budget_bytes):
    family=load_family();archives=[]
    try:
        with outer_source(inputs,row,audit) as (source,expected,part_charge,chain):
            source.seek(0,io.SEEK_END);outer_size=source.tell();source.seek(0)
            entries,_,central_offset=audit._zip_layout(source,outer_size)
            with zipfile.ZipFile(source) as outer:
                need(len(outer.infolist())==entries and outer.start_dir==central_offset,
                     'Outer census ZIP central-directory count/offset differs')
                audit._checkpoint(force=True)
                tars=[info for info in outer.infolist() if info.filename.endswith(('.tar.gz','.tar.xz'))]
                need(len(tars)==1,'Expected one current Maven payload TAR in family')
                for info in tars:
                    family.core.safe_name(info.filename)
                    if expected is not None:
                        need(info.filename in expected,'TAR member absent from authenticated index');identity=expected[info.filename]
                    else:identity={}
                    codec='xz' if info.filename.endswith('.xz') else 'gzip'
                    with outer.open(info) as stream:
                        facts=census_tar_headers(stream,codec,chain=[*chain,info.filename],audit=audit,
                            expected_bytes=identity.get('bytes',info.file_size),expected_sha256=identity.get('sha256'),
                            part_scratch_charge_bytes=part_charge,metadata_budget_bytes=metadata_budget_bytes)
                    archives.append({'path':info.filename,'codec':codec,**facts,
                        'compressedIdentityBinding':'Exact authenticated index member' if expected is not None else
                            'Official whole service ZIP SHA and consumed member CRC; no selected repack identity claim'})
        return {'familyId':row['original']['id'],'familyName':row['original']['name'],'route':row['route'],
            'archives':archives,'capacityAdmitted':all(archive['capacityAdmitted'] for archive in archives),
            'observedProcessPeakRssBytes':audit.peak_rss,'observedScratchPeakBytes':audit.peak_scratch,
            'nestedZIPBodiesMaterialized':False,'actualFamilyAcceptanceClaimed':False}
    except CensusError:raise
    except (zipfile.BadZipFile,family.core.AuditError,EOFError,OSError,ValueError) as error:raise CensusError(str(error)) from error


def sealed_json(path,value):
    raw=(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode()
    need(len(raw)<=MAX_METADATA,'Census metadata output exceeds2MiB')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400)
    with os.fdopen(fd,'wb') as target:target.write(raw);target.flush();os.fsync(target.fileno())
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);os.fsync(fd);os.close(fd)
    reference=source_reference(path);read_reference(reference,json_value=False);return reference


def run_plan(plan_ref,output):
    family=load_family();intent=policy.current();plan=validate_plan(read_reference(plan_ref))
    approval=verify_source_approval(plan['sourceApproval'])
    inputs=bound_inputs(plan['catalogue'],plan['materializationMap'])
    expected=maven_family_names(intent);observed=[inputs.families[aid]['original']['name'] for aid in plan['mavenFamilyIds']]
    need(observed==expected,'Census plan family sequence does not exactly cover the current Maven matrix')
    scratch=Path(plan['scratchRoot']);st=os.lstat(scratch)
    need(stat.S_ISDIR(st.st_mode) and st.st_uid==os.getuid() and stat.S_IMODE(st.st_mode)==0o700,'Census scratch root must be owned0700')
    os.mkdir(output,0o700)
    invocation={'schemaVersion':1,'kind':'GDAL-cohort-header-census-invocation',
        'startedAtUtc':dt.datetime.now(dt.timezone.utc).isoformat(),'plan':plan_ref,'cohortManifest':policy.current_ref(),
        **{key:intent['scope'][key] for key in ('head','tree','base')},'sources':approval['sources'],
        'pid':os.getpid(),'uid':os.getuid(),'prospectiveLimits':plan['prospectiveLimits'],'capabilityAccepted':False}
    invocation_ref=sealed_json(output/'invocation.json',invocation)
    results=[];remaining=MAX_METADATA-65536
    try:
        for aid in plan['mavenFamilyIds']:
            with tempfile.TemporaryDirectory(prefix='c23-header-census-',dir=scratch) as temporary:
                limits=intent['resources']
                audit=family.core.Audit(family.core.AuditConfig(scratch_root=temporary,
                    max_nested_bytes=limits['nestedArchiveLimitBytes'],max_scratch_bytes=limits['scratchLimitBytes'],
                    rss_limit=limits['rssLimitBytes']),lambda record:None)
                row=census_family(inputs,inputs.families[aid],audit,remaining)
                charge=len(json.dumps(row,ensure_ascii=False,indent=2).encode())+512
                need(charge<=remaining,'Aggregate census metadata reservation exceeded');remaining-=charge;results.append(row)
                print(json.dumps({'familyCompleted':len(results),'familyName':row['familyName'],'capacityAdmitted':row['capacityAdmitted']}),flush=True)
        for reference in [plan_ref,plan['catalogue'],plan['materializationMap'],plan['sourceApproval'],*approval['sources']]:
            read_reference(reference,json_value=False)
        admitted=all(row['capacityAdmitted'] for row in results)
        outcome={'status':'completed-header-census','capacityAdmitted':admitted}
    except BaseException as error:
        outcome={'status':'failed-header-census','capacityAdmitted':False,'errorType':type(error).__name__,'error':str(error)}
        raise_after=error
    else:raise_after=None
    entries=[entry for row in results for archive in row['archives'] for entry in archive['storedNestedArchives']]
    report={'schemaVersion':1,'kind':'GDAL-cohort-current-Maven-header-census',
        'completedAtUtc':dt.datetime.now(dt.timezone.utc).isoformat(),'invocation':invocation_ref,
        'cohortManifest':policy.current_ref(),'source':plan['censusSource'],'catalogue':plan['catalogue'],
        'materializationMap':plan['materializationMap'],'families':results,'actualMavenFamilies':len(results),
        'storedNestedArchives':len(entries),'maximumStoredNestedArchiveBytes':max((entry['declaredStoredArchiveBytes'] for entry in entries),default=0),
        'prospectiveLimits':plan['prospectiveLimits'],**outcome,'nestedZIPBodiesMaterialized':False,
        'recursiveNestedZIPPeakProven':False,'actualFamilyAcceptanceClaimed':False,'capabilityAccepted':False,'overallGoalComplete':False}
    result_ref=sealed_json(output/'result.json',report)
    print(json.dumps({'report':result_ref,'capacityAdmitted':report['capacityAdmitted']}),flush=True)
    if raise_after is not None:raise raise_after
    return 0 if report['capacityAdmitted'] else 2


def main():
    parser=argparse.ArgumentParser(description=__doc__);policy.add_arguments(parser)
    sub=parser.add_subparsers(dest='command',required=True)
    prepare=sub.add_parser('prepare-plan',help='Bind concrete pinned metadata inputs without opening raw bodies')
    prepare.add_argument('--catalogue-ref',required=True);prepare.add_argument('--materialization-map-ref',required=True)
    prepare.add_argument('--source-approval-ref',required=True);prepare.add_argument('--scratch-root',required=True,type=Path)
    prepare.add_argument('--output',required=True,type=Path)
    run=sub.add_parser('run',help='ROOT-only invocation after source approval and concrete input plan review')
    run.add_argument('--plan',required=True,type=Path);run.add_argument('--plan-sha256',required=True)
    run.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();policy.initialize_for_process()
    if args.command=='prepare-plan':
        plan=compile_plan(json.loads(args.catalogue_ref),json.loads(args.materialization_map_ref),json.loads(args.source_approval_ref),args.scratch_root)
        validate_plan(plan);print(json.dumps({'plan':sealed_json(args.output,plan)}));return 0
    reference={'path':str(args.plan),'bytes':os.lstat(args.plan).st_size,'sha256':args.plan_sha256}
    return run_plan(reference,args.output)


if __name__=='__main__':raise SystemExit(main())

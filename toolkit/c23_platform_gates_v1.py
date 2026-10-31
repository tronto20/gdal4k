"""C17 platform evidence joins driven by the strict current cohort manifest."""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import xml.etree.ElementTree as ET

M = 1048576
WINDOWS_HELPERS = (
    'native_consumer_diagnostics.py','windows_dump_quota.py','windows_native_stall.py',
    'windows_process.py','consumer.py','consumer_evidence.py','command_evidence.py',
    'ci-diagnostics.init.gradle',
)
WINDOWS_TESTS = (
    'test_embed_data.py','test_command_evidence.py','test_windows_process.py','test_windows_pe.py',
    'test_windows_winsock_link.py','test_windows_native_stall.py','test_consumer_retention_phases.py',
    'test_windows_owned_members.py','test_windows_dump_quota.py','test_native_consumer_diagnostics.py',
)
WINDOWS_BATCH_COUNTS = (4,7,12,6,1,20,41,34,50,26)
CALLBACK_LAYOUT = {'pointerBytes':8,'inputPid':0,'inputHandle':4,'inputType':12,'inputUnion':16,'ioHandle':0,'ioOffset':8,'ioBuffer':16,'ioBytes':24,'ioProjectionBytes':28,'outputStatus':0,'informationParam':8}
SDK_REFERENCE = 'bd614402842ff7f890d96a5312416600c6c0d2ee2ddd3f7b8dac0961e5814d1f'

def _need(c, condition, message):
    c.require(bool(condition), message)

def _path(record):
    return record['chain'][-1]

def _ref(record):
    return {k: record[k] for k in ('familyId','familyName','chain','bytes','sha256')}

def _source(c, relative):
    path = Path(c.source_root) / relative
    fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME|os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        _need(c,stat.S_ISREG(before.st_mode),'current source is not a regular file')
        _need(c, before.st_size <= 2*M, 'source file exceeds bounded source-read policy')
        data = b''
        while len(data) <= 2*M:
            chunk = os.read(fd,65536)
            if not chunk: break
            data += chunk
        after = os.fstat(fd)
        fields = lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mode,s.st_nlink,s.st_uid,s.st_gid,s.st_atime_ns,s.st_mtime_ns,s.st_ctime_ns,s.st_blocks)
        _need(c,fields(before)==fields(after)==fields(os.lstat(path)),'current source identity changed during read')
        return data, {'path':relative,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    finally:
        os.close(fd)

def _identity(c):
    value=c.cohort['scope']
    _need(c,isinstance(value,dict),'validated cohort scope is absent')
    return value


def _run(c, label):
    value=_identity(c)[label+'Run']
    _need(c,type(value) is int and value>0,'cohort run ID is not a positive integer')
    return value


def _contract(c):
    groups=c.cohort['contracts']['common']
    _need(c,isinstance(groups,list) and groups,'current common test contract is absent')
    expected={};sources=[]
    for group in groups:
        name,path,methods=group['className'],group['path'],group['methods']
        _need(c,isinstance(name,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',name) and name not in expected,'duplicate or invalid common contract class')
        _need(c,isinstance(path,str) and path.startswith('gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/') and path.endswith('/'+name+'.kt') and '..' not in PurePosixPath(path).parts,'common contract path differs from its exact source class')
        _need(c,isinstance(methods,list) and methods and len(methods)==len(set(methods)) and all(isinstance(x,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',x) for x in methods),'common contract methods are invalid or duplicated')
        raw,pin=_source(c,path)
        actual=re.findall(r'@Test\s+fun\s+(\w+)\s*\(',raw.decode('utf-8'))
        _need(c,actual==methods and len(actual)==len(set(actual)),'current common source methods differ from the pinned cohort contract: '+name)
        expected[name]=set(methods);sources.append({'className':name,**pin,'methods':list(methods)})
    return expected,{'groups':sources,'methodCount':sum(map(len,expected.values()))}


def _case_inventory(c, pairs, expected, label):
    found={name:[] for name in expected}
    for name,method in pairs:
        if name in found:found[name].append(method)
    _need(c,all(set(found[name])==methods and len(found[name])==len(methods) for name,methods in expected.items()),label+' class/method contract cases are missing, duplicated or unexpected')
    return found


def _scope(c, prior=('sourceBindings','maven')):
    s=c.scope;identity=_identity(c)
    _need(c,all(s.get(key)==identity[key] for key in ('head','tree','base','baseTree','testCheckout')),'platform gate has wrong current H/T/B/checkout scope')
    _need(c,s.get('checkoutTree')==identity['tree'] and s.get('checkoutParents')==[identity['base'],identity['head']],'platform gate has wrong actual test checkout identity')
    _need(c,all(name in c.prior for name in prior),'required prior semantic gate is absent')
    expected_roles=c.cohort['ci']['requiredRoles'];expected={}
    for label in ('primary','profile'):
        names=expected_roles[label]
        _need(c,isinstance(names,list) and names and len(names)==len(set(names)),'cohort role inventory is invalid')
        for name in names:
            _need(c,isinstance(name,str) and name and name not in expected,'cohort role name is duplicated')
            expected[name]=_run(c,label)
    rows=c.role_checks
    _need(c,len(rows)==len(expected) and len({row['id'] for row in rows})==len(rows) and {row['name'] for row in rows}==set(expected),'qualified current CI role inventory differs')
    runs=s.get('runs')
    _need(c,isinstance(runs,list) and len(runs)==2 and all(type(x.get('id')) is int and x['id']>0 for x in runs) and len({x['id'] for x in runs})==2,'qualified current run inventory differs')
    runs={row['id']:row for row in runs}
    _need(c,set(runs)=={_run(c,'primary'),_run(c,'profile')},'qualified current run IDs differ')
    for run in runs.values():
        _need(c,type(run.get('attempt')) is int and run['attempt']>0 and run.get('status')=='completed' and run.get('conclusion')=='success','qualified run did not complete successfully')
    for row in rows:
        _need(c,type(row['id']) is int and row['id']>0 and type(row['runId']) is int and row['runId']==expected[row['name']] and type(row['attempt']) is int and row['attempt']==runs[row['runId']]['attempt'] and row.get('head')==identity['head'] and row['status']=='completed' and row['conclusion']=='success','required CI role identity/run attempt/status differs')
    return rows


def _role(c, name, run):
    rows=[row for row in c.role_checks if row['name']==name and row['runId']==run]
    _need(c,len(rows)==1,'missing or ambiguous actual qualified CI role: '+name)
    return dict(rows[0])


def _records(c, families, predicate):
    output=[]
    for family in families:
        _need(c,family in c.families,'required artifact family is absent: '+family)
        for record in c.records(family=family):
            if predicate(record):
                output.append(record)
                _need(c,len(output)<=8192,'selected platform metadata cardinality exceeds bounded policy')
    return output

def _one(c, records, description):
    _need(c,len(records)==1,'missing or ambiguous '+description)
    return records[0]

def _suffix(c, families, suffix):
    return _records(c,families,lambda r:_path(r)==suffix or _path(r).endswith('/'+suffix))

def _bulk(c, records):
    # Split only at the public 8MiB metadata bound. read_many groups each batch
    # into forward family/TAR reads instead of replaying each member prefix.
    seen=set();out={};batch=[];size=0
    for r in records:
        key=c.key(r)
        if key in seen:continue
        seen.add(key)
        _need(c,type(r['bytes']) is int and 0<=r['bytes']<=2*M,'platform metadata member exceeds 2MiB')
        if batch and size+r['bytes']>8*M:
            out.update(_bulk_once(c,batch));batch=[];size=0
        batch.append(r);size+=r['bytes']
    if batch:out.update(_bulk_once(c,batch))
    return out

def _bulk_once(c, records):
    value=c.read_many(records,max_file_bytes=2*M,max_total_bytes=8*M)
    expected={c.key(r) for r in records}
    _need(c,set(value)==expected,'bounded reader returned missing or extra platform metadata')
    for r in records:
        body=value[c.key(r)]
        _need(c,isinstance(body,bytes) and len(body)==r['bytes'] and hashlib.sha256(body).hexdigest()==r['sha256'],'bounded metadata bytes do not match audited member identity')
    return value

def _json(c, record, bodies):
    def unique(pairs):
        value={}
        for key,item in pairs:
            _need(c,key not in value,'duplicate producer JSON key')
            value[key]=item
        return value
    value=json.loads(bodies[c.key(record)].decode('utf-8-sig'),object_pairs_hook=unique)
    _need(c,isinstance(value,dict),'producer evidence must be a JSON object')
    return value

def _jsons(c, records):
    bodies=_bulk(c,records)
    return {c.key(r):_json(c,r,bodies) for r in records}

def _clean_log(value):
    if isinstance(value,bytes):value=value.decode('utf-8-sig')
    value=value.removeprefix('\ufeff').replace('\r\n','\n')
    value=re.sub(r'^\d{4}-\d\d-\d\dT\S+Z ?','',value,flags=re.M)
    return re.sub(r'\x1b\[[0-9;]*[A-Za-z]','',value)

def _native_suite(c, text, namespace, expected):
    text=_clean_log(text)
    starts=re.findall(r'^\[==========\] Running (\d+) tests from (\d+) test cases\.$',text,re.M)
    ends=re.findall(r'^\[==========\] (\d+) tests from (\d+) test cases ran\. .*$',text,re.M)
    totals=re.findall(r'^\[  PASSED  \] (\d+) tests\.$',text,re.M)
    runs=re.findall(r'^\[ RUN      \] (\S+)\s*$',text,re.M)
    passed=re.findall(r'^\[       OK \] (\S+) \([^\n]*\)\s*$',text,re.M)
    _need(c,len(starts)==len(ends)==len(totals)==1 and starts==ends,'incomplete or multiple actual Native suites')
    count=int(starts[0][0])
    _need(c,count>0 and count==int(totals[0])==len(runs)==len(passed) and runs==passed and len(set(runs))==count,'Native RUN/PASS case inventory disagrees with suite totals')
    _need(c,not re.search(r'^\[\s*(?:FAILED|SKIPPED)\s*\]',text,re.M),'Native suite contains failed or skipped tests')
    pairs=[]
    for case in runs:
        prefix=namespace+'.'
        if case.startswith(prefix):
            body=case[len(prefix):]
            if '.' in body:pairs.append(tuple(body.split('.',1)))
    cases=_case_inventory(c,pairs,expected,'Native '+namespace)
    return {'tests':count,'testCases':int(starts[0][1]),'failures':0,'errors':0,'skipped':0,'contractCases':cases,'allCases':runs}


def _xml_contract(c, records, expected, namespace, fixture=False):
    bodies=_bulk(c,records);pairs=[];fixtures=[];used=[];totals={'tests':0,'failures':0,'errors':0,'skipped':0}
    for record in records:
        raw=bodies[c.key(record)]
        _need(c,b'<!DOCTYPE' not in raw.upper() and b'<!ENTITY' not in raw.upper(),'DTD/entity-bearing JUnit evidence is not allowed')
        tree=ET.fromstring(raw.decode('utf-8-sig'))
        _need(c,tree.tag in ('testsuite','testsuites'),'unexpected JUnit root')
        declared=0
        for suite in tree.iter('testsuite'):
            if any(child.tag=='testsuite' for child in suite):continue
            testcase_count=sum(child.tag=='testcase' for child in suite)
            _need(c,int(suite.get('tests','0'))==testcase_count,'JUnit declared testcase count differs from its actual elements')
            declared+=testcase_count
            for field in totals:totals[field]+=int(suite.get(field,'0'))
            for field in ('failures','errors','skipped'):_need(c,int(suite.get(field,'0'))==0,'JUnit suite contains '+field)
        touched=False
        _need(c,declared==sum(1 for _ in tree.iter('testcase')),'JUnit testcase is outside its declared leaf suite')
        for case in tree.iter('testcase'):
            _need(c,not any(case.find(tag) is not None for tag in ('failure','error','skipped')),'retained JUnit context contains a failed/skipped testcase')
            kind=case.get('classname','');name=case.get('name','').split('[',1)[0].strip().removesuffix('()')
            class_name=kind[len(namespace)+1:] if namespace and kind.startswith(namespace+'.') else (kind.rsplit('.',1)[-1] if namespace=='' else None)
            if class_name in expected:
                pairs.append((class_name,name));touched=True
            if kind==namespace+'.DriverProfileApiTest':fixtures.append(name);touched=True
        if touched:used.append(_ref(record))
    cases=_case_inventory(c,pairs,expected,'JUnit '+namespace)
    if fixture:_need(c,fixtures==['requiredDriversAndRealDataRoundTrip'],'NC4 profile entrypoint is absent or duplicated')
    return {'contractCases':cases,'suiteCounts':totals,'evidence':used,**({'profileEntrypoint':fixtures[0]} if fixture else {})}


def _gradle_contract(c, text, expected, namespace, target):
    text=_clean_log(text)
    if namespace:
        pattern=r'^'+re.escape(namespace)+r'\.([A-Za-z_][A-Za-z0-9_]*)\.(\w+)\['+re.escape(target)+r'\] (PASSED|FAILED|SKIPPED)\s*$'
    else:
        pattern=r'^([A-Za-z_][A-Za-z0-9_]*)\['+re.escape(target)+r'\] > (\w+)\['+re.escape(target)+r'\] (PASSED|FAILED|SKIPPED)\s*$'
    pairs=[]
    for name,method,status in re.findall(pattern,text,re.M):
        if name in expected:
            _need(c,status=='PASSED','current Gradle contract case failed or skipped')
            pairs.append((name,method))
    return _case_inventory(c,pairs,expected,'Gradle '+(namespace or 'published JVM'))


def _consumer(c, value, target, *, compiled=False, classifier=None, hidden=False, version=None):
    _need(c,value.get('result')=='passed' and value.get('target')==target,'consumer target/result differs')
    _need(c,value.get('compiled_only') is compiled and value.get('expected_missing_binary') is False,'consumer execution/negative-control scope differs')
    _need(c,isinstance(value.get('version'),str) and value['version'],'consumer publication version is missing')
    expected_version=c.default_version if version is None else version
    _need(c,isinstance(expected_version,str) and expected_version and value['version']==expected_version,'consumer publication version differs from its qualified Maven/profile scope')
    if classifier is not None:
        _need(c,value.get('jvm_binaries')==[classifier],'positive JVM consumer did not select exactly the current classifier')
        files=value.get('runtime_files',[])
        engines=[name for name in files if name.startswith('gdal4k-engine-') and name.endswith('.jar')]
        _need(c,len(engines)==1 and engines[0].endswith('-'+classifier+'.jar'),'positive JVM runtime payload selection differs')
    if hidden:
        _need(c,value.get('producer_paths_hidden') is True and value.get('producer_paths_hidden_count')==3,'independent Native consumer did not hide the three producer inputs')
        platform={'linuxArm64':'linux-arm64','linuxX64':'linux-amd64','macosArm64':'macos-arm64','mingwX64':'windows-amd64','iosArm64':'ios-arm64','iosSimulatorArm64':'ios-simulator-arm64'}[target]
        names=value.get('producer_paths_hidden_names',[])
        _need(c,isinstance(names,list) and len(names)==3 and all(isinstance(name,str) for name in names),'independent consumer hidden path list differs')
        if target=='mingwX64':names=[name.replace('\\','/') for name in names]
        _need(c,set(names)=={'gdal4k-engine/build/engine/'+platform,'gdal4k-engine/build/classes/kotlin/'+target+'/main/cinterop','gdal4k-runtime/build/classes/kotlin/'+target+'/main/cinterop'},'independent consumer hidden producer paths differ')
    return {k:value[k] for k in ('target','version','compiled_only','result')}

def _consumer_rows(c, families):
    return _records(c,families,lambda r:('/verification/results/published-consumer-' in '/'+_path(r) and _path(r).endswith('/result.json')) or (r['familyName'] in {'evidence-linux-arm64-runtime','evidence-jvm-payload-selection','evidence-published-jvm-macos-15','evidence-published-jvm-windows-2025'} and _path(r)=='published-consumer-jvm/result.json'))

def _xml_for(c, family, prefix):
    return _records(c,[family],lambda r:_path(r).startswith(prefix) and _path(r).endswith('.xml') and ('/test-results/' in _path(r) or '/androidTest-results/' in _path(r)))

def _positive_jvm(c, family, classifier, expected, *, version=None, fixture=False):
    rows=_consumer_rows(c,[family]);values=_jsons(c,rows)
    matches=[r for r in rows if values[c.key(r)].get('target')=='jvm' and values[c.key(r)].get('jvm_binaries')==[classifier] and values[c.key(r)].get('expected_missing_binary') is False]
    record=_one(c,matches,'positive single-classifier JVM receipt '+classifier)
    value=values[c.key(record)];facts=_consumer(c,value,'jvm',classifier=classifier,version=version)
    prefix=_path(record).rsplit('/',1)[0]+'/'
    tests=_xml_contract(c,_xml_for(c,family,prefix),expected,'consumer',fixture=fixture)
    return {'classifier':classifier,'receipt':_ref(record),'consumer':facts,'tests':tests}

def gate_default_arm64(c):
    _scope(c);expected,source=_contract(c)
    role=_role(c,'verify / linux-arm64-runtime',_run(c,'primary'))
    text=_clean_log(c.job_logs['defaultArm64'])
    _need(c,'python verification/ci.py linuxArm64-run' in text and ('JAVA_HOME_21_ARM64' in text or 'aarch64' in text),'actual default ARM execution log lacks its current role/host markers')
    starts=[m.start() for m in re.finditer(r'^\[==========\] Running ',text,re.M)]
    _need(c,len(starts)==2,'default ARM log must contain exactly source and published Native suites')
    native=[]
    for i,namespace in enumerate(('dev.gdal4k','consumer')):
        end=starts[i+1] if i+1<len(starts) else len(text)
        block=text[starts[i]:end]
        finish=re.search(r'^\[  PASSED  \] \d+ tests\.$',block,re.M)
        _need(c,finish is not None,'default ARM Native suite is incomplete')
        native.append(_native_suite(c,block[:finish.end()],namespace,expected))
    jvm_cases=_gradle_contract(c,text,expected,'','jvm')
    published=_positive_jvm(c,'evidence-linux-arm64-runtime','linux-arm64-gnu',expected)
    compiled_rows=_suffix(c,['evidence-linuxArm64'],'verification/results/published-consumer-linuxArm64/result.json')
    record=_one(c,compiled_rows,'cross-produced default ARM Native compilation receipt')
    value=_jsons(c,[record])[c.key(record)];_consumer(c,value,'linuxArm64',compiled=True,hidden=True)
    binaries=_records(c,['engine-linuxArm64'],lambda r:_path(r).endswith('/test.kexe'))
    source_bins=[r for r in binaries if '/gdal4k-runtime/build/bin/linuxArm64/debugTest/' in '/'+_path(r)]
    consumer_bins=[r for r in binaries if '/verification/results/published-consumer-linuxArm64/bin/' in '/'+_path(r)]
    for rows in (source_bins,consumer_bins):
        binary=_one(c,rows,'staged AArch64 Native test executable')
        _need(c,binary.get('details',{}).get('format')=='ELF' and binary['details'].get('machine')==183 and binary['details'].get('bits')==64,'default ARM executable header is not ELF64 AArch64')
    return {'head':_identity(c)['head'],'role':role,'sourceContract':source,'nativeSuiteTotalsInExecutionOrder':[row['tests'] for row in native],'sourceNative':native[0],'publishedNative':native[1],'publishedJvm':published,'publishedJvmLogCases':jvm_cases,'crossCompilationReceipt':_ref(record),'stagedExecutableHeaderFacts':[_ref(r) for r in source_bins+consumer_bins],'defaultSourceJVMTestsExecutedByThisRole':False,'scope':'Actual source Native, independently published Native and published JVM on Linux ARM64; source JVM was not run by this default role. Header facts do not establish a compiled SDK ABI.'}

def _relative_record(c, family, prefix, relative, description):
    _need(c,isinstance(relative,str) and relative and not relative.startswith('/') and '..' not in PurePosixPath(relative).parts,'unsafe relative producer evidence path')
    records=_records(c,[family],lambda r:_path(r)==prefix+relative)
    return _one(c,records,description)

def _passed_command(c, value, log_record, *, executable=None):
    _need(c,value.get('status')=='passed' and value.get('returncode')==0 and type(value.get('returncode')) is int and value.get('outputComplete') is True,'actual command did not complete successfully')
    _need(c,value.get('logSha256')==log_record['sha256'] and value.get('log')==PurePosixPath(_path(log_record)).name,'actual command log binding differs')
    _need(c,type(value.get('pid')) is int and value['pid']>0 and value.get('startedAtUtc') and value.get('completedAtUtc'),'actual command process/time identity missing')
    started=datetime.datetime.fromisoformat(value['startedAtUtc']);ended=datetime.datetime.fromisoformat(value['completedAtUtc'])
    _need(c,started.tzinfo is not None and ended.tzinfo is not None and ended>=started,'actual command UTC ordering differs')
    _need(c,type(value.get('elapsedSeconds')) in (int,float) and math.isfinite(value['elapsedSeconds']) and value['elapsedSeconds']>0,'actual command duration missing')
    if executable is not None:_need(c,value.get('command')==[executable],'actual Native launch differs from retained executable path')

def gate_combined_arm64(c):
    _scope(c);expected,source=_contract(c)
    role=_role(c,'Actual combined JVM and Native on Linux ARM64',_run(c,'profile'))
    family='profile-evidence-combined-linux-arm64';report_family='profile-combined-linux-arm64'
    report_record=_one(c,_records(c,[report_family],lambda r:_path(r).endswith('/report.json')),'combined ARM current report')
    native_records=[_one(c,_suffix(c,[family],'verification/results/arm64-native-'+label+'.json'),'combined ARM '+label+' runtime receipt') for label in ('source','consumer')]
    restored_record=_one(c,_suffix(c,[family],'verification/results/arm64-transfer-restored.json'),'combined ARM restored transfer')
    initial=_jsons(c,[report_record,restored_record,*native_records]);report=initial[c.key(report_record)];restored=initial[c.key(restored_record)]
    _need(c,report.get('profile')=='combined' and report.get('target')=='linux-arm64','combined ARM report scope differs')
    transfer=restored['transfer']
    _need(c,restored.get('actualHost')=={'system':'Linux','machine':'aarch64'} and transfer.get('producerHost')=={'system':'Linux','machine':'x86_64'},'combined transfer/execution host scopes differ')
    _need(c,transfer.get('profile')=='combined' and transfer.get('target')=='linux-arm64' and transfer.get('git')=={'commit':_identity(c)['testCheckout'],'tree':_identity(c)['tree']},'combined ARM transfer checkout identity differs')
    binding=report['arm64RuntimeBindings'];_need(c,binding['transfer']['restoredReceiptSha256']==restored_record['sha256'] and binding['transfer']['transfer']==transfer,'combined report is not bound to the retained transfer')
    details=[]
    for label,record,namespace in zip(('source','consumer'),native_records,('dev.gdal4k','consumer')):
        value=initial[c.key(record)]
        _need(c,value.get('result')=='passed' and value.get('actual_execution') is True and value.get('compiled_only') is False and value.get('target')=='linux-arm64' and value.get('host')=={'system':'Linux','machine':'aarch64'},'combined ARM Native execution receipt differs')
        prefix=_path(record).split('verification/results/arm64-native-',1)[0]
        command_record=_relative_record(c,family,prefix,value['command'],'Native actual command')
        log_record=_relative_record(c,family,prefix,value['log'],'Native actual log')
        _need(c,command_record['sha256']==value['commandSha256'] and log_record['sha256']==value['logSha256'],'combined Native command/log SHA differs')
        bodies=_bulk(c,[command_record,log_record]);command=_json(c,command_record,bodies)
        executable=command.get('command',[None])[0]
        _passed_command(c,command,log_record,executable=executable)
        _need(c,command.get('timeoutSeconds')==1800 and isinstance(executable,str) and executable.endswith('/'+value['executable']),'combined ARM launch path or unchanged timeout differs')
        _need(c,command.get('cwd')==executable[:-len(value['executable'])].rstrip('/'),'combined Native launch cwd differs from its actual snapshot')
        counts=_native_suite(c,bodies[c.key(log_record)],namespace,expected)
        recorded=value['counts']
        _need(c,all(recorded.get(k)==counts[k] for k in ('tests','failures','errors','skipped')) and recorded.get('cases')==counts['allCases'],'combined Native reported counts differ from actual stdout')
        _need(c,namespace+'.DriverProfileApiTest.requiredDriversAndRealDataRoundTrip' in counts['allCases'],'combined actual Native NC4 entrypoint is absent')
        _need(c,report['tests'][label+'Native']=={k:counts[k] for k in ('tests','failures','errors','skipped')},'combined profile report Native counts differ')
        binary_path=transfer[label+'Binary'];binary_meta=transfer['files'][binary_path]
        _need(c,binary_path==transfer['output']+'/source/'+value['executable'],'transferred executable path differs from its current runtime path')
        _need(c,binary_meta['sha256']==value['executableSha256'],'combined transferred Native executable SHA differs')
        binaries=_suffix(c,['profile-cross-combined-linux-arm64'],binary_path)
        binary=_one(c,[r for r in binaries if r['sha256']==binary_meta['sha256'] and r['bytes']==binary_meta['bytes']],'unchanged cross-produced actual Native executable')
        _need(c,binary.get('details',{}).get('format')=='ELF' and binary['details'].get('machine')==183 and binary['details'].get('bits')==64,'combined executable header is not ELF64 AArch64')
        _need(c,binding['native'][label]['receiptSha256']==record['sha256'] and binding['native'][label]['commandSha256']==command_record['sha256'] and binding['native'][label]['logSha256']==log_record['sha256'],'combined report Native bindings differ')
        if label=='consumer':
            compiled=value['compilationReceipt'];compile_record=_relative_record(c,family,prefix,compiled['path'],'unchanged combined Native compile-only receipt')
            _need(c,compile_record['sha256']==compiled['sha256'],'compile-only receipt identity was rewritten')
            compile_value=_jsons(c,[compile_record])[c.key(compile_record)];_consumer(c,compile_value,'linuxArm64',compiled=True,hidden=True,version=report['version'])
            transferred_compile=transfer['output']+'/source/'+compiled['path']
            _need(c,transfer['files'][transferred_compile]['sha256']==compile_record['sha256'],'actual runtime compile-only receipt differs from the immutable cross-transfer input')
            original_compile=_one(c,_suffix(c,['profile-cross-combined-linux-arm64'],transferred_compile),'original cross-produced compile-only receipt')
            _need(c,original_compile['sha256']==compile_record['sha256'] and original_compile['bytes']==compile_record['bytes'],'cross-produced compilation receipt was rewritten for the runtime report')
        else:_need(c,value.get('compilationReceipt') is None,'source Native carries an unexpected consumer compilation receipt')
        details.append({'scope':label,'runtimeReceipt':_ref(record),'command':_ref(command_record),'log':_ref(log_record),'executable':_ref(binary),'actualCases':counts,'originalCompilationReceiptPreserved':label=='consumer'})
    jvm=_positive_jvm(c,family,'linux-arm64-gnu',expected,version=report['version'],fixture=True)
    source_xml=_records(c,[family],lambda r:'/gdal4k-runtime/build/test-results/jvmTest/' in '/'+_path(r) and _path(r).endswith('.xml'))
    source_jvm=_xml_contract(c,source_xml,expected,'dev.gdal4k',fixture=True)
    _need(c,source_jvm['suiteCounts']==report['tests']['sourceJvm'],'combined ARM source JVM producer counts differ from actual retained JUnit')
    _need(c,jvm['tests']['suiteCounts']==report['tests']['consumerJvm'],'combined ARM published JVM producer counts differ from actual retained JUnit')
    return {'head':_identity(c)['head'],'role':role,'sourceContract':source,'report':_ref(report_record),'transferRestoration':_ref(restored_record),'actualNativeLaunches':details,'sourceJvm':source_jvm,'publishedJvm':jvm,'earlierCrossBuildReceiptCompiledOnlyPreserved':True,'compileOnlyReceiptRewritten':False,'scope':'Actual combined source JVM, published JVM, source Native and published Native on AArch64; the original cross-produced Native consumer receipt remains compile-only. NC4 assertions are inside the current source-bound profile entrypoint.'}

def _windows_batches(c,text):
    text=_clean_log(text)
    first=text.find('python -m unittest discover -s verification -p '+WINDOWS_TESTS[0])
    end=text.find('--build-engine-only',first)
    _need(c,first>=0 and end>first,'current early Windows helper gate command range is absent')
    region=text[first:end]
    commands=re.findall(r'python -m unittest discover -s verification -p (test_[a-z_]+\.py)',region)
    # The action renders the first command in its group title and again in its
    # command text. Collapse only that adjacent exact duplicate.
    collapsed=[]
    for command in commands:
        if not collapsed or command!=collapsed[-1]:collapsed.append(command)
    _need(c,tuple(collapsed)==WINDOWS_TESTS,'actual Windows helper command list differs from current committed source')
    matches=re.findall(r'^Ran (\d+) tests? in [^\n]+\n(?:\s*\n)*OK(?:\n|$)',region,re.M)
    _need(c,tuple(map(int,matches))==WINDOWS_BATCH_COUNTS,'actual Windows helper batch totals/statuses differ')
    _need(c,not re.search(r'^OK\s*\(|^FAILED|^ERROR|skipped=|\(skipped=',region,re.M),'Windows helper gate has a failure or skipped tests')
    return [{'module':name,'tests':count,'failures':0,'errors':0,'skipped':0} for name,count in zip(WINDOWS_TESTS,WINDOWS_BATCH_COUNTS)]

def _dump(c,raw):
    _need(c,32<=len(raw)<=2*M,'retained diagnostic minidump exceeds bounded gate input')
    signature,version,count,offset,checksum,stamp,flags=struct.unpack_from('<IIIIIIQ',raw)
    _need(c,signature==0x504d444d and 0<count<=4096 and offset+12*count<=len(raw) and not flags&2,'retained minidump header/directory differs')
    kinds=[]
    for i in range(count):
        kind,amount,position=struct.unpack_from('<III',raw,offset+12*i)
        _need(c,position+amount<=len(raw),'minidump stream exceeds retained payload')
        if kind in (3,4,17):_need(c,amount>0 and kind not in kinds,'required minidump stream empty/duplicate')
        kinds.append(kind)
    _need(c,{3,4,17}<=set(kinds),'retained minidump lacks thread/module/thread-info streams')
    return {'bytes':len(raw),'flags':flags,'streamCount':count,'streamTypes':kinds,'checkedHeaderAndStreamDirectory':True,'fullContextOrStackValidationClaimed':False}

def _readiness(c,value,quota_source_sha):
    _need(c,value.get('status')=='passed' and value.get('actualWindowsOwnedChildSelfCheck') is True and value.get('sourceSha256')==quota_source_sha,'actual Windows owned self-check source/status differs')
    _need(c,value.get('compiledSdkAbiVerified') is False and value.get('fullMemoryRequested') is False,'self-check incorrectly upgrades ABI/full-memory scope')
    _need(c,value.get('callbackLayoutProjection')==CALLBACK_LAYOUT,'self-check packed4 callback projection differs from current source')
    ready=value['targetReadiness']
    _need(c,ready.get('protocol')=='owned-private-pipe-v1' and ready.get('readyBeforeFirstDump') is True and ready.get('markerAndEofObserved') is True,'owned child private-pipe readiness is incomplete')
    _need(c,type(ready.get('targetPid')) is int and ready['targetPid']>0 and type(ready.get('markerBytes')) is int and 0<ready['markerBytes']<=60 and re.fullmatch('[0-9a-f]{64}',ready.get('markerSha256','')) and ready.get('enclosingWorkerDeadlineCeilingSeconds')==20,'private-pipe readiness identity/bound differs')
    _need(c,type(value.get('successfulDumpBytes')) is int and 0<value['successfulDumpBytes']<=8*M and type(value.get('rejectedDumpBytes')) is int and 0<=value['rejectedDumpBytes']<=16,'self-check dump byte bounds differ')
    success=value['success'];failure=value['failure']
    for row,limit in ((success,8*M),(failure,16)):
        _need(c,row.get('quotaBytes')==limit and row.get('compiledSdkAbiVerified') is False and row.get('ordinaryFileHandleFallback') is False and row.get('requestedDumpFlags')==0x1000,'callback quota/flags/ABI scope differs')
        _need(c,row.get('callbackLayoutProjection')==CALLBACK_LAYOUT and row.get('sdkSourceReferenceSha256')==SDK_REFERENCE and row.get('maxCopyChunkBytes')==M and row.get('callbackTypeCardinalityLimit')==32 and row.get('callbackCountLimit')==0xffffffff and row.get('callbackTypeOverflowEvents')==0 and row.get('callbackCountOverflowEvents')==0,'callback projection/status/copy and count bounds differ from current source')
        _need(c,row.get('miniDumpApiCalled') is True and row.get('miniDumpReturnedNormally') is True and type(row.get('miniDumpBooleanReturn')) is bool and type(row.get('apiLastErrorUnsigned32')) is int and re.fullmatch(r'0x[0-9a-f]{8}',row.get('apiLastErrorHex','')),'observed API boolean/HRESULT fields are missing')
        raw_error=row.get('apiLastErrorObserved')
        _need(c,type(raw_error) is int and row['apiLastErrorUnsigned32']==raw_error&0xffffffff and row['apiLastErrorHex']==f'0x{raw_error&0xffffffff:08x}' and type(row.get('miniDumpReturnCode')) is int and row['miniDumpReturnCode']==int(row['miniDumpBooleanReturn']),'observed API boolean/HRESULT projection disagrees with retained raw values')
        device=row['nulDevice'];_need(c,device.get('ordinaryFileFallback') is False and device.get('fileTypeObserved')==2 and device.get('nativeDumpWrittenOnlyByBoundedCallbacks') is True,'callback sink did not retain its fixed NUL character-device policy')
    _need(c,success.get('miniDumpBooleanReturn') is True and success.get('ioStartObserved') is True and success.get('ioFinishObserved') is True and success.get('callbackError') is None and success.get('ioWriteAllCallbacks',0)>0 and success.get('extentBytes')==value['successfulDumpBytes'],'successful actual callback dump receipt differs')
    _need(c,all(type(success.get('callbackTypeCounts',{}).get(str(kind))) is int and success['callbackTypeCounts'][str(kind)]>0 for kind in (11,12,13)),'successful dump lacks the actual start/write/finish callback sequence')
    _need(c,failure.get('callbackError',{}).get('type')=='QuotaExceeded' and 0<=failure.get('extentBytes',-1)<=16 and failure.get('extentBytes')==value['rejectedDumpBytes'],'actual 16-byte quota negative control is absent')


def _transient_dump_report(c, value):
    # current committed source checks the real files in its owned worker, then unlinks both in
    # finally before serializing the source-bound self-check receipt.
    report=value.get('successDumpDirectory',{})
    _need(c,set(report)=={'bytes','flags','streamCount','streamTypes','checkedHeaderAndStreamDirectory','fullContextOrStackValidationClaimed'},'transient self-check directory report schema differs')
    _need(c,type(report.get('bytes')) is int and 32<=report['bytes']<=8*M and report['bytes']==value['successfulDumpBytes'],'transient self-check directory byte bound differs')
    _need(c,type(report.get('flags')) is int and 0<=report['flags']<2**64 and not report['flags']&2,'transient self-check directory requests full memory')
    kinds=report.get('streamTypes')
    _need(c,type(report.get('streamCount')) is int and 0<report['streamCount']<=4096 and isinstance(kinds,list) and len(kinds)==report['streamCount'] and all(type(kind) is int and 0<=kind<2**32 for kind in kinds),'transient self-check stream count/types differ')
    _need(c,all(kinds.count(kind)==1 for kind in (3,4,17)) and report.get('checkedHeaderAndStreamDirectory') is True and report.get('fullContextOrStackValidationClaimed') is False,'transient self-check required streams/scope differ')
    return {'producerDirectoryReport':report,'successfulDumpBytes':value['successfulDumpBytes'],'rejectedDumpBytes':value['rejectedDumpBytes'],'rawSelfCheckDumpBytesRetained':False,'rawSelfCheckDumpPhysicallyReceived':False,'evidenceScope':'Source-bound actual owned-worker report; current committed source deletes both transient self-check dump files in finally. No retained self-check dump bytes are claimed.'}

def _retained_capture(c, family, native_prefix):
    rows=_records(c,[family],lambda r:_path(r).startswith(native_prefix) and re.fullmatch(r'capture-[0-9]+/collector\.json',_path(r)[len(native_prefix):]) is not None)
    record=_one(c,rows,'separate retained Windows diagnostic collector')
    value=_jsons(c,[record])[c.key(record)]
    _need(c,value.get('status')=='captured' and value.get('ownedJobMembershipVerified') is True and value.get('collectorOutsideTarget') is True and value.get('rootCauseOrRuntimeAcceptance') is False,'separate Windows diagnostic collector scope differs')
    dump=value.get('dump',{});prefix=_path(record).rsplit('/',1)[0]+'/'
    dump_path=dump.get('path','')
    _need(c,isinstance(dump_path,str) and ntpath.isabs(dump_path) and '..' not in PurePosixPath(dump_path.replace('\\','/')).parts and dump_path.replace('\\','/').endswith('/'+prefix+'native.dmp') and dump.get('fullMemoryRequested') is False,'separate diagnostic dump path/scope differs')
    member=_one(c,_records(c,[family],lambda r:_path(r)==prefix+'native.dmp'),'separate retained Windows diagnostic dump')
    _need(c,type(dump.get('bytes')) is int and dump['bytes']==member['bytes'] and dump.get('sha256')==member['sha256'],'separate retained Windows diagnostic dump binding differs')
    facts=_dump(c,_bulk(c,[member])[c.key(member)])
    _need(c,type(dump.get('flags')) is int and facts['flags']==dump['flags'],'separate retained Windows diagnostic dump header flags differ')
    return {'collector':_ref(record),'dump':_ref(member),'checkedDirectory':facts,'evidenceScope':'Separate retained diagnostic capture; these bytes are not the deleted owned-worker self-check dumps and do not establish a runtime root cause.'}


def _native_observer_ledger(c, raw, record, command, launch, native_prefix, capture_records):
    """Bind the current source-qualified observer ledger to its successful Job."""
    diag=command.get('nativeConsumerDiagnostics')
    _need(c,type(diag) is dict and set(diag)=={'eventCount','eventsFile','eventsSha256',
        'launchIdentityRetained','captureRounds','rootCauseOrRuntimeAcceptance'},'Native observer summary schema differs')
    _need(c,type(diag['eventCount']) is int and diag['eventCount']>0 and
        type(diag['captureRounds']) is int and 0<=diag['captureRounds']<=2 and
        diag['launchIdentityRetained'] is True and diag['rootCauseOrRuntimeAcceptance'] is False,
        'Native observer summary values differ')
    reported=diag['eventsFile']
    _need(c,type(reported) is str and ntpath.isabs(reported) and
        '..' not in PurePosixPath(reported.replace('\\','/')).parts and
        reported.replace('\\','/').endswith('/'+native_prefix+'events.jsonl') and
        _path(record)==native_prefix+'events.jsonl' and diag['eventsSha256']==record['sha256'] and
        len(raw)==record['bytes'] and hashlib.sha256(raw).hexdigest()==record['sha256'],
        'Native observer ledger path/whole bytes differ')
    # These limits are the committed native_consumer_diagnostics.py constants.
    _need(c,0<len(raw)<=320*1024 and raw.endswith(b'\n'),'Native observer ledger bounds/framing differ')
    def unique(pairs):
        value={}
        for key,item in pairs:
            _need(c,key not in value,'Duplicate Native observer JSON key');value[key]=item
        return value
    def invalid(value):
        raise ValueError('Nonfinite Native observer JSON constant: '+value)
    rows=[];previous=-1
    for index,line in enumerate(raw.splitlines(),1):
        _need(c,0<len(line)<=65536,'Native observer JSONL record bound differs')
        row=json.loads(line,object_pairs_hook=unique,parse_constant=invalid)
        _need(c,type(row) is dict and type(row.get('sequence')) is int and row['sequence']==index and
            type(row.get('monotonic')) in (int,float) and math.isfinite(row['monotonic']) and
            row['monotonic']>=0 and row['monotonic']>=previous and
            type(row.get('kind')) is str and bool(row['kind']),'Native observer sequence/clock/kind differs')
        previous=row['monotonic'];rows.append(row)
    _need(c,len(rows)==diag['eventCount'],'Native observer summary count differs from ledger')
    closed=[row for row in rows if row['kind']=='observer-closed']
    _need(c,len(closed)==1 and closed[0] is rows[-1] and
        set(closed[0])=={'sequence','monotonic','kind','captureRounds','rootCauseOrRuntimeAcceptance'} and
        type(closed[0]['captureRounds']) is int and closed[0]['captureRounds']==diag['captureRounds'] and
        closed[0]['rootCauseOrRuntimeAcceptance'] is False,'Native observer terminal closeout differs')
    verified=[row for row in rows if row['kind']=='native-launch-intent-verified']
    discovered=[row for row in rows if row['kind']=='native-child-discovered']
    _need(c,len(verified)==len(discovered)==1 and verified[0]['sequence']<discovered[0]['sequence'] and
        type(launch.get('executable')) is str and ntpath.isabs(launch['executable']) and
        '..' not in PurePosixPath(launch['executable'].replace('\\','/')).parts and
        verified[0].get('executable')==launch.get('executable') and
        type(verified[0].get('chargedBytes')) is int and
        type(launch.get('conservativeChargedBytes')) is int and
        verified[0]['chargedBytes']==launch.get('conservativeChargedBytes'),
        'Native observer launch is not the retained source-bound launch')
    target=discovered[0].get('target')
    _need(c,type(target) is dict and set(target)=={'pid','creationTime100ns','imagePath'} and
        type(target['pid']) is int and 0<target['pid']<2**32 and
        type(target['creationTime100ns']) is int and 0<target['creationTime100ns']<2**64 and
        type(target['imagePath']) is str and ntpath.isabs(target['imagePath']) and
        ntpath.normcase(ntpath.normpath(target['imagePath']))==ntpath.normcase(ntpath.normpath(launch['executable'])),
        'Native observer child process does not match the retained launch')
    process=command.get('windowsProcess');cleanup=command.get('cleanup');ownership=command.get('ownership')
    _need(c,command.get('status')=='passed' and type(command.get('returncode')) is int and
        command['returncode']==0 and command.get('outputComplete') is True and
        type(ownership) is dict and set(ownership)=={'kind','assignedBeforeResume','killOnClose'} and
        ownership['kind']=='Windows Job Object' and ownership['assignedBeforeResume'] is True and
        ownership['killOnClose'] is True and
        command.get('pidRole')=='owned child process' and type(command.get('pid')) is int and
        type(process) is dict and type(process.get('pid')) is int and process['pid']==command['pid'] and
        type(process.get('creationTime100ns')) is int and process['creationTime100ns']>0 and
        type(process.get('imagePath')) is str and ntpath.isabs(process['imagePath']) and
        type(cleanup) is dict and type(cleanup.get('activeProcessesAfter')) is int and
        cleanup['activeProcessesAfter']==0 and cleanup.get('jobClosed') is True and
        cleanup.get('forcedTreeTermination') is True,'Native observer command was not a successful closed owned Job')
    captures=[row for row in rows if row['kind'].startswith('capture-')]
    positive=diag['captureRounds']>0 or bool(captures) or bool(capture_records)
    if positive:
        _need(c,diag['captureRounds']>0 and bool(captures) and bool(capture_records),
            'Positive Native capture evidence cannot use the zero-capture branch')
    else:
        _need(c,diag['captureRounds']==0,'Zero Native capture branch differs')
    return {'eventLedger':_ref(record),'eventCount':len(rows),'captureRounds':diag['captureRounds'],
        'terminalObserverClosed':True,'retainedLaunchBound':True,'successfulOwnedCommandBound':True,
        'positiveCaptureEvidence':positive,'rootCauseOrRuntimeAcceptance':False}


def gate_windows_readiness(c):
    _scope(c);expected,contract=_contract(c)
    role=_role(c,'verify / Native mingwX64',_run(c,'primary'))
    batches=_windows_batches(c,c.job_logs['windowsReadiness'])
    source_pins={}
    for name in WINDOWS_HELPERS:
        _,pin=_source(c,'verification/'+name);source_pins[pin['path']]=pin
    family='evidence-mingwX64'
    full_record=_one(c,_suffix(c,[family],'writer-selfcheck/self-check.json'),'actual late Windows callback full self-check')
    native_prefix=_path(full_record).rsplit('/writer-selfcheck/self-check.json',1)[0]+'/'
    required={name:_one(c,_records(c,[family],lambda r,name=name:_path(r)==native_prefix+name),'Windows Native '+name) for name in ('callback-quota-ready.json','context.json','launch-intent.json')}
    values=_jsons(c,[full_record,*required.values()]);full=values[c.key(full_record)]
    _readiness(c,full,source_pins['verification/windows_dump_quota.py']['sha256'])
    ready=values[c.key(required['callback-quota-ready.json'])]
    _need(c,ready=={'status':'passed','actualWindowsOwnedChildSelfCheck':True,'sourceSha256':full['sourceSha256'],'compiledSdkAbiVerified':False},'reduced readiness differs from the actual source-qualified self-check')
    context=values[c.key(required['context.json'])];launch=values[c.key(required['launch-intent.json'])]
    _need(c,launch.get('task')=='mingwX64Test' and launch.get('sourceBindings')==context.get('sourceBindings') and launch.get('retainedByHardlink') is True and launch.get('additionalPayloadAllocationBytes')==0 and launch.get('requestedArgumentsAreNotObservedNativeArgv') is True,'published Native launch/retention source binding differs')
    _need(c,type(launch.get('executableBytes')) is int and launch['executableBytes']>0 and launch.get('budgetBytes')==context.get('budgetBytes')==512*M and launch.get('conservativeChargedBytes',512*M+1)<=512*M,'published Native launch diagnostic budget differs')
    bindings={row['path']:row for row in context['sourceBindings'] if row.get('sourceScope')=='verification-helper'}
    _need(c,set(bindings)==set(source_pins),'Windows helper source binding closure differs')
    for name,pin in source_pins.items():_need(c,bindings[name]['sha256']==pin['sha256'] and bindings[name]['bytes']==pin['bytes'],'Windows helper source changed: '+name)
    selfcheck_dump=_transient_dump_report(c,full)
    evidence_root=context['evidenceRoot'];relative=ntpath.relpath(launch['retainedExecutable'],evidence_root).replace('\\','/')
    _need(c,not relative.startswith('../') and not ntpath.isabs(relative),'retained Native launch escapes its evidence root')
    evidence_prefix=native_prefix.split('verification/results/published-consumer-mingwX64/',1)[0]+'verification/results/published-consumer-mingwX64/'
    binary=_one(c,_records(c,[family],lambda r:_path(r)==evidence_prefix+relative),'retained independently published Windows Native executable')
    _need(c,binary['bytes']==launch['executableBytes'] and binary['sha256']==launch['executableSha256'] and binary.get('details',{}).get('format')=='PE' and binary['details'].get('machine')==0x8664,'retained consumer PE AMD64 header/payload differs')
    consumer_record=_one(c,_records(c,[family],lambda r:_path(r)==evidence_prefix+'result.json'),'independent Windows Native consumer result')
    consumer=_jsons(c,[consumer_record])[c.key(consumer_record)];_consumer(c,consumer,'mingwX64',hidden=True)
    command_records=_records(c,[family],lambda r:_path(r).startswith(evidence_prefix+'commands/') and _path(r).endswith('.json'))
    commands=_jsons(c,command_records)
    matching=[r for r in command_records if 'mingwX64Test' in commands[c.key(r)].get('command',[])]
    command_record=_one(c,matching,'actual independent Windows Native command completion')
    command=commands[c.key(command_record)];log_record=_relative_record(c,family,_path(command_record).rsplit('/',1)[0]+'/',command['log'],'actual independent Windows Native command stdout')
    _passed_command(c,command,log_record)
    _need(c,command.get('ownership')=={'kind':'Windows Job Object','assignedBeforeResume':True,'killOnClose':True},'independent Native command Job ownership differs')
    event_record=_one(c,_records(c,[family],lambda r:_path(r)==native_prefix+'events.jsonl'),'source-bound Native observer event ledger')
    capture_records=_records(c,[family],lambda r:_path(r).startswith(native_prefix) and
        _path(r)[len(native_prefix):].split('/',1)[0].startswith('capture-'))
    observer=_native_observer_ledger(c,_bulk(c,[event_record])[c.key(event_record)],event_record,
        command,launch,native_prefix,capture_records)
    if observer['positiveCaptureEvidence']:
        retained_capture=_retained_capture(c,family,native_prefix)
    else:
        retained_capture={'physicallyReceived':False,'captureRounds':0,
            'evidenceScope':'The source-bound observer closed with zero capture rounds; no diagnostic dump bytes are claimed.'}
    body=_clean_log(_bulk(c,[log_record])[c.key(log_record)])
    passes=_gradle_contract(c,body,expected,'consumer','mingwX64')
    return {'head':_identity(c)['head'],'role':role,'sourceContract':contract,'currentSourceBindings':list(source_pins.values()),'earlyHelperBatches':batches,'earlyHelperTests':201,'earlyQuotaTests':50,'actualFullSelfCheck':_ref(full_record),'actualPrivatePipeReadiness':full['targetReadiness'],'transientSelfCheckDump':selfcheck_dump,'retainedDiagnosticCapture':retained_capture,'nativeObserver':observer,'callbackReady':_ref(required['callback-quota-ready.json']),'launch':_ref(required['launch-intent.json']),'retainedNativeBinary':_ref(binary),'consumerResult':_ref(consumer_record),'consumerCommand':_ref(command_record),'consumerStdout':_ref(log_record),'publishedNativeContractCases':passes,'compiledSdkAbiVerified':False,'previousOSHRESULTCauseProven':False,'requestedArgumentsNotUpgradedToObservedNativeArgv':True,'scope':'Current cohort Windows helper gate, owned private-pipe callback readiness, source-bound transient bounded dump report plus quota16 refusal, a terminal Native observer ledger with zero or checked retained captures, and successful independent published Native consumer. SDK ABI, previous HRESULT cause and performance are not established.'}

def _receipt_for_target(c, family, target, *, browser=None):
    records=_consumer_rows(c,[family]);values=_jsons(c,records)
    matches=[r for r in records if values[c.key(r)].get('target')==target and (browser is None or values[c.key(r)].get('browser') is browser)]
    record=_one(c,matches,'published consumer '+target+' in '+family)
    return record,values[c.key(record)]

def gate_platform_scopes(c):
    _scope(c,('sourceBindings','maven','defaultArm64','combinedArm64','windowsReadiness'))
    expected,contract=_contract(c)
    classifiers=[]
    for family,classifier in [('evidence-jvm-payload-selection','linux-x64-gnu'),('evidence-linux-arm64-runtime','linux-arm64-gnu'),('evidence-published-jvm-macos-15','macos-arm64'),('evidence-published-jvm-windows-2025','windows-x64')]:
        classifiers.append(_positive_jvm(c,family,classifier,expected))
    native=[];source_jvm=[]
    for target,family in [('linuxX64','evidence-linuxX64'),('macosArm64','evidence-macosArm64'),('mingwX64','evidence-mingwX64'),('iosSimulatorArm64','evidence-iosSimulatorArm64')]:
        role=_role(c,'verify / Native '+target,_run(c,'primary'))
        source_records=_records(c,[family],lambda r:'/gdal4k-runtime/build/test-results/'+target+'Test/' in '/'+_path(r) and _path(r).endswith('.xml'))
        source_cases=_xml_contract(c,source_records,expected,'dev.gdal4k')
        result_record,result=_receipt_for_target(c,family,target);_consumer(c,result,target,hidden=True)
        consumer_cases=_xml_contract(c,_xml_for(c,family,_path(result_record).rsplit('/',1)[0]+'/'),expected,'consumer')
        native.append({'target':target,'role':role,'sourceCases':source_cases,'publishedCases':consumer_cases,'consumerReceipt':_ref(result_record),'execution':'actual Native runtime'})
        if target in ('linuxX64','macosArm64','mingwX64'):
            xml=_records(c,[family],lambda r:'/gdal4k-runtime/build/test-results/jvmTest/' in '/'+_path(r) and _path(r).endswith('.xml'))
            source_jvm.append({'host':target,'tests':_xml_contract(c,xml,expected,'dev.gdal4k')})
    ios_record,ios=_receipt_for_target(c,'evidence-iosArm64','iosArm64');_consumer(c,ios,'iosArm64',compiled=True,hidden=True)
    web=[]
    for target in ('js','wasmJs'):
        for browser,label in ((False,'Node'),(True,'Chromium')):
            task=target+('BrowserTest' if browser else 'NodeTest')
            source_xml=_records(c,['evidence-web'],lambda r:'/gdal4k-runtime/build/test-results/'+task+'/' in '/'+_path(r) and _path(r).endswith('.xml'))
            source_cases=_xml_contract(c,source_xml,expected,'dev.gdal4k')
            record,value=_receipt_for_target(c,'evidence-web',target,browser=browser);_consumer(c,value,target)
            consumer_cases=_xml_contract(c,_xml_for(c,'evidence-web',_path(record).rsplit('/',1)[0]+'/'),expected,'consumer')
            web.append({'target':target,'host':label,'sourceCases':source_cases,'publishedCases':consumer_cases,'consumerReceipt':_ref(record)})
    android_family='evidence-android-arm64-v8a'
    android_record=_one(c,_suffix(c,[android_family],'gdal4k-runtime/build/results/android.json'),'actual Android emulator bridge report')
    android=_jsons(c,[android_record])[c.key(android_record)]
    _need(c,android.get('status')=='passed' and android.get('abi')=='arm64-v8a' and android.get('deviceAbi')=='x86_64' and android.get('translated') is True and android.get('nativeBridge') not in ('','0',None) and type(android.get('tests')) is int and android['tests']>=sum(map(len,expected.values())) and bool(android.get('apks')),'Android actual native-bridge runtime scope differs')
    android_xml=_records(c,[android_family],lambda r:'/gdal4k-runtime/build/outputs/androidTest-results/' in '/'+_path(r) and _path(r).endswith('.xml'))
    android_source=_xml_contract(c,android_xml,expected,'dev.gdal4k')
    android_consumer_record,android_consumer=_receipt_for_target(c,android_family,'android');_consumer(c,android_consumer,'android')
    android_published=_xml_contract(c,_xml_for(c,android_family,_path(android_consumer_record).rsplit('/',1)[0]+'/'),expected,'consumer')
    nc4=[]
    for profile,target,native_target in [('scientific','linux-amd64','linuxX64'),('scientific','macos-arm64','macosArm64'),('combined','linux-amd64','linuxX64'),('combined','macos-arm64','macosArm64'),('combined','windows-amd64','mingwX64')]:
        family='profile-evidence-'+profile+'-'+target;report_family='profile-'+profile+'-'+target
        report_record=_one(c,_records(c,[report_family],lambda r:_path(r).endswith('/report.json')),'current NC4 profile report')
        report=_jsons(c,[report_record])[c.key(report_record)]
        _need(c,report.get('profile')==profile and report.get('target')==target,'NC4 profile report target differs')
        contexts={}
        for label,task,namespace in [('sourceJvm','jvmTest','dev.gdal4k'),('sourceNative',native_target+'Test','dev.gdal4k')]:
            rows=_records(c,[family],lambda r,task=task:'/gdal4k-runtime/build/test-results/'+task+'/' in '/'+_path(r) and _path(r).endswith('.xml'))
            contexts[label]=_xml_contract(c,rows,expected,namespace,fixture=True)
        for label,consumer_target in [('consumerJvm','jvm'),('consumerNative',native_target)]:
            result_record,result=_receipt_for_target(c,family,consumer_target);_consumer(c,result,consumer_target,hidden=consumer_target!='jvm',version=report['version'])
            contexts[label]=_xml_contract(c,_xml_for(c,family,_path(result_record).rsplit('/',1)[0]+'/'),expected,'consumer',fixture=True)
        for label in contexts:
            counts=report['tests'][label]
            _need(c,type(counts.get('tests')) is int and counts['tests']>0 and all(counts.get(k)==0 for k in ('failures','errors','skipped')),'NC4 profile context contains failed/skipped tests')
            _need(c,counts==contexts[label]['suiteCounts'],'NC4 producer report totals differ from actual retained JUnit cases')
        nc4.append({'profile':profile,'target':target,'report':_ref(report_record),'contexts':contexts})
    _,fixture_source=_source(c,'verification/driver-profile/DriverProfileApiTest.kt')
    return {'head':_identity(c)['head'],'sourceContract':contract,'defaultFourJVMClassifiers':classifiers,'defaultSourceJVMs':source_jvm,'defaultSourceJVMARM64Executed':False,'nativeRuntimeScopes':native,'defaultARMActualRuntimePrior':'defaultArm64','optionalCombinedSourceJVMARM64SeparatelyVerified':'combinedArm64','iosDeviceCompileOnly':{'consumerReceipt':_ref(ios_record),'physicalDeviceExecuted':False,'scope':'Current iOS ARM64 source/consumer compile and link only'},'webRuntimeScopes':web,'androidRuntimeScope':{'report':_ref(android_record),'sourceCases':android_source,'consumerReceipt':_ref(android_consumer_record),'publishedCases':android_published,'scope':'ARM64 library execution through x86_64 emulator native bridge','physicalARM64DeviceExecuted':False},'nc4ProfileRuntimeScopes':nc4,'combinedARMNC4Prior':'combinedArm64','currentNC4FixtureSource':fixture_source,'compiledSDKABIEstablished':False,'externalPublicationEstablished':False,'fullGDALGoalComplete':False,'scope':'Current configured API and regression contracts across four JVM classifiers and supported Native/web/Android routes; precise compile-only, emulator and actual runtime scopes remain separate.'}

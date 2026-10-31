"""Strict pinned static cohort intent. No remote/artifact/result facts belong here."""
from __future__ import annotations
import copy,hashlib,json,os,re,stat,subprocess,sys
from pathlib import Path,PurePosixPath

MiB=1048576
MAX_INTENT=2*MiB
_NATIVE=('gdal4k/native/','gdal4k-engine/ports/','gdal4k-engine/engine/','gdal4k-engine/tools/','gdal4k-engine/web/')
_KEYS={'schemaVersion','kind','cohort','feature','scope','source','ci','apiMethods','contracts','packages','familyNames','resources'}
_RESOURCES={'rssLimitBytes':128*MiB,'nestedArchiveLimitBytes':128*MiB,'scratchLimitBytes':192*MiB,'captureFileLimitBytes':2*MiB,'captureBatchLimitBytes':8*MiB,'proofBudgetBytes':256*MiB,'workspaceFloorBytes':32*MiB,'metadataReservationBytes':96*MiB,'hostFloorBytes':2*1024**3,'tmpFloorBytes':2*1024**3,'shmFloorBytes':2*1024**3,'cgroupCacheReserveBytes':128*MiB,'prospectiveHostCgroupReservationBytes':416*MiB}
_CURRENT=None
_REF=None
class IntentError(ValueError):pass
def need(condition,message):
 if not condition:raise IntentError(message)
def full11(s):return {k:getattr(s,k) for k in ('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_atime_ns','st_mtime_ns','st_ctime_ns','st_blocks')}
def parse_json(raw):
 def pairs(rows):
  value={}
  for k,v in rows:need(k not in value,'Duplicate intent JSON key');value[k]=v
  return value
 return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(IntentError('Nonfinite intent JSON')))
def integer(value,minimum=1,maximum=2**53-1):return type(value) is int and minimum<=value<=maximum
def string(value,maximum=512):return isinstance(value,str) and 0<len(value.encode('utf-8'))<=maximum and not any(ord(c)<32 or ord(c)==127 for c in value)
def safe_relative(value):
 need(string(value,4096) and '\\' not in value and ':' not in value,'Invalid intent relative path')
 p=PurePosixPath(value);need(not p.is_absolute() and p.as_posix()==value and all(x not in ('','.','..') for x in p.parts),'Noncanonical intent relative path');return value
def absolute(value):
 need(string(value,4096) and Path(value).is_absolute() and str(Path(value))==value and '..' not in Path(value).parts,'Noncanonical intent absolute path');return value
def hexstr(value,length):return isinstance(value,str) and re.fullmatch('[0-9a-f]{'+str(length)+'}',value) is not None
def exact(value,keys,label):need(type(value) is dict and set(value)==set(keys),label+' field inventory differs')
def read_bytes(path,maximum=MAX_INTENT,sealed=False):
 absolute(str(path));fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME|os.O_NONBLOCK)
 with os.fdopen(fd,'rb') as f:
  before=full11(os.fstat(f.fileno()));need(stat.S_ISREG(before['st_mode']) and before['st_nlink']==1 and 0<=before['st_size']<=maximum,'Pinned intent/input size or regular-file identity differs')
  if sealed:need(stat.S_IMODE(before['st_mode'])==0o400 and before['st_uid']==os.getuid(),'Static intent must be sealed owner file')
  raw=f.read(maximum+1);need(len(raw)==before['st_size'] and full11(os.fstat(f.fileno()))==before==full11(os.lstat(path)),'Pinned input full11 changed')
 return raw
def validate_reference(reference,maximum=MAX_INTENT):
 exact(reference,('path','bytes','sha256'),'Pinned reference');absolute(reference['path']);need(integer(reference['bytes'],0,maximum) and hexstr(reference['sha256'],64),'Pinned reference byte identity differs')
 return reference
def read_pin(reference,maximum=MAX_INTENT,sealed=False):
 validate_reference(reference,maximum)
 raw=read_bytes(reference['path'],maximum,sealed);need(len(raw)==reference['bytes'] and hashlib.sha256(raw).hexdigest()==reference['sha256'],'Pinned input whole bytes/SHA differs');return raw
def _source_refs(rows):
 need(type(rows) is list and rows,'Static source references missing');by={}
 for r in rows:
  exact(r,('path','bytes','sha256','gitBlob','gitMode'),'Source reference');name=safe_relative(r['path']);need(name not in by and integer(r['bytes'],0,8*MiB) and hexstr(r['sha256'],64) and hexstr(r['gitBlob'],40) and r['gitMode'] in ('100644','100755'),'Static source reference identity differs');by[name]=r
 need(list(by)==sorted(by),'Static source reference ordering differs');return by

_API_SOURCE='gdal4k/src/commonMain/kotlin/dev/gdal4k/runtime/MDArray.kt'
_API_FIELDS=('logicalId','name','parameterTypes','returnType','jvmName',
             'descriptor','genericSignature','wireKey','contractRole')
_API_CONTRACTS=({'logicalId': 'MDArray.getUnscaled()',
  'name': 'getUnscaled',
  'parameterTypes': [],
  'returnType': 'MDArray',
  'jvmName': 'getUnscaled',
  'descriptor': '(Lkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(Lkotlin/coroutines/Continuation<-Ldev/gdal4k/runtime/MDArray;>;)Ljava/lang/Object;',
  'wireKey': 'getUnscaled',
  'contractRole': 'feature'},
 {'logicalId': 'MDArray.getView(String)',
  'name': 'getView',
  'parameterTypes': ['String'],
  'returnType': 'MDArray',
  'jvmName': 'getView',
  'descriptor': '(Ljava/lang/String;Lkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(Ljava/lang/String;Lkotlin/coroutines/Continuation<-Ldev/gdal4k/runtime/MDArray;>;)Ljava/lang/Object;',
  'wireKey': 'getView_String',
  'contractRole': 'feature'},
 {'logicalId': 'MDArray.getMask(List<String>)',
  'name': 'getMask',
  'parameterTypes': ['List<String>'],
  'returnType': 'MDArray',
  'jvmName': 'getMask',
  'descriptor': '(Ljava/util/List;Lkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(Ljava/util/List<Ljava/lang/String;>;Lkotlin/coroutines/Continuation<-Ldev/gdal4k/runtime/MDArray;>;)Ljava/lang/Object;',
  'wireKey': 'getMask_List_String',
  'contractRole': 'feature'},
 {'logicalId': 'MDArray.getCoordinateVariables()',
  'name': 'getCoordinateVariables',
  'parameterTypes': [],
  'returnType': 'List<MDArray>',
  'jvmName': 'getCoordinateVariables',
  'descriptor': '(Lkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(Lkotlin/coroutines/Continuation<-Ljava/util/List<+Ldev/gdal4k/runtime/MDArray;>;>;)Ljava/lang/Object;',
  'wireKey': 'getCoordinateVariables',
  'contractRole': 'feature'},
 {'logicalId': 'MDArray.getStatistics(Boolean,Boolean)',
  'name': 'getStatistics',
  'parameterTypes': ['Boolean', 'Boolean'],
  'returnType': 'MDStatistics?',
  'jvmName': 'getStatistics',
  'descriptor': '(ZZLkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(ZZLkotlin/coroutines/Continuation<-Ldev/gdal4k/runtime/MDStatistics;>;)Ljava/lang/Object;',
  'wireKey': 'getStatistics_Boolean_Boolean',
  'contractRole': 'feature'},
 {'logicalId': 'MDArray.computeStatistics(Boolean,List<String>)',
  'name': 'computeStatistics',
  'parameterTypes': ['Boolean', 'List<String>'],
  'returnType': 'MDStatistics',
  'jvmName': 'computeStatistics',
  'descriptor': '(ZLjava/util/List;Lkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(ZLjava/util/List<Ljava/lang/String;>;Lkotlin/coroutines/Continuation<-Ldev/gdal4k/runtime/MDStatistics;>;)Ljava/lang/Object;',
  'wireKey': 'computeStatistics_Boolean_List_String',
  'contractRole': 'feature'},
 {'logicalId': 'MDArray.getResampled(List<Dimension?>,RasterResampling,SpatialReference?,List<String>)',
  'name': 'getResampled',
  'parameterTypes': ['List<Dimension?>', 'RasterResampling', 'SpatialReference?', 'List<String>'],
  'returnType': 'MDArray',
  'jvmName': 'getResampled',
  'descriptor': '(Ljava/util/List;Ldev/gdal4k/runtime/RasterResampling;Ldev/gdal4k/runtime/SpatialReference;Ljava/util/List;Lkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(Ljava/util/List<+Ldev/gdal4k/runtime/Dimension;>;Ldev/gdal4k/runtime/RasterResampling;Ldev/gdal4k/runtime/SpatialReference;Ljava/util/List<Ljava/lang/String;>;Lkotlin/coroutines/Continuation<-Ldev/gdal4k/runtime/MDArray;>;)Ljava/lang/Object;',
  'wireKey': 'getResampled_List_Dimension_RasterResampling_SpatialReference_List_String',
  'contractRole': 'feature'},
 {'logicalId': 'MDArray.getTotalElementsCount()',
  'name': 'getTotalElementsCount',
  'parameterTypes': [],
  'returnType': 'ULong',
  'jvmName': 'getTotalElementsCount-I7RO_PI',
  'descriptor': '(Lkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(Lkotlin/coroutines/Continuation<-Lkotlin/ULong;>;)Ljava/lang/Object;',
  'wireKey': 'getTotalElementsCount',
  'contractRole': 'feature'},
 {'logicalId': 'MDArray.getProcessingChunkSize(ULong)',
  'name': 'getProcessingChunkSize',
  'parameterTypes': ['ULong'],
  'returnType': 'List<ULong>',
  'jvmName': 'getProcessingChunkSize-4PLdz1A',
  'descriptor': '(JLkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(JLkotlin/coroutines/Continuation<-Ljava/util/List<Lkotlin/ULong;>;>;)Ljava/lang/Object;',
  'wireKey': 'getProcessingChunkSize_ULong',
  'contractRole': 'feature'},
 {'logicalId': 'MDArray.setScale(Double)',
  'name': 'setScale',
  'parameterTypes': ['Double'],
  'returnType': 'Boolean',
  'jvmName': 'setScale',
  'descriptor': '(DLkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(DLkotlin/coroutines/Continuation<-Ljava/lang/Boolean;>;)Ljava/lang/Object;',
  'wireKey': 'setScale_Double',
  'contractRole': 'legacyCompatibility'},
 {'logicalId': 'MDArray.setOffset(Double)',
  'name': 'setOffset',
  'parameterTypes': ['Double'],
  'returnType': 'Boolean',
  'jvmName': 'setOffset',
  'descriptor': '(DLkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(DLkotlin/coroutines/Continuation<-Ljava/lang/Boolean;>;)Ljava/lang/Object;',
  'wireKey': 'setOffset_Double',
  'contractRole': 'legacyCompatibility'},
 {'logicalId': 'MDArray.setScale(Double,Int)',
  'name': 'setScale',
  'parameterTypes': ['Double', 'Int'],
  'returnType': 'Boolean',
  'jvmName': 'setScale',
  'descriptor': '(DILkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(DILkotlin/coroutines/Continuation<-Ljava/lang/Boolean;>;)Ljava/lang/Object;',
  'wireKey': 'setScale_Double_Int',
  'contractRole': 'feature'},
 {'logicalId': 'MDArray.setOffset(Double,Int)',
  'name': 'setOffset',
  'parameterTypes': ['Double', 'Int'],
  'returnType': 'Boolean',
  'jvmName': 'setOffset',
  'descriptor': '(DILkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(DILkotlin/coroutines/Continuation<-Ljava/lang/Boolean;>;)Ljava/lang/Object;',
  'wireKey': 'setOffset_Double_Int',
  'contractRole': 'feature'},
 {'logicalId': 'MDArray.setSpatialRef(SpatialReference?)',
  'name': 'setSpatialRef',
  'parameterTypes': ['SpatialReference?'],
  'returnType': 'Boolean',
  'jvmName': 'setSpatialRef',
  'descriptor': '(Ldev/gdal4k/runtime/SpatialReference;Lkotlin/coroutines/Continuation;)Ljava/lang/Object;',
  'genericSignature': '(Ldev/gdal4k/runtime/SpatialReference;Lkotlin/coroutines/Continuation<-Ljava/lang/Boolean;>;)Ljava/lang/Object;',
  'wireKey': 'setSpatialRef_SpatialReference',
  'contractRole': 'feature'})

def method_contract_key(method):
 """The classfile identity includes the descriptor; overload names may repeat."""
 return method['jvmName'],method['descriptor']

def api_method_contracts():
 """Fresh approved static source-preparation data, never remote/runtime facts."""
 return copy.deepcopy(list(_API_CONTRACTS))

def validate_api_methods(methods):
 """Pin each full logical signature to its complete reviewed JVM/wire/role row."""
 need(type(methods) is list and len(methods)==len(_API_CONTRACTS),
      'Exactly fourteen public method contracts required: twelve feature and two legacyCompatibility')
 expected={m['logicalId']:m for m in _API_CONTRACTS};seen=set();seen_jvm=set()
 for method in methods:
  exact(method,_API_FIELDS,'API method')
  need(all(type(method[k]) is str for k in _API_FIELDS if k!='parameterTypes') and
       type(method['parameterTypes']) is list and all(type(t) is str for t in method['parameterTypes']),
       'Public method contract field types differ')
  logical=method['logicalId'];need(logical in expected and logical not in seen,
       'Unknown/duplicate public logical signature contract')
  need(method==expected[logical],
       'Fixed logical signature/JVM descriptor/Signature/wire/role mapping differs: '+logical)
  key=method_contract_key(method);need(key not in seen_jvm,'Duplicate public JVM name/descriptor pair')
  seen.add(logical);seen_jvm.add(key)
 need(seen==set(expected),'Public full-signature contract inventory differs')
 return methods

def _kotlin_code(raw):
 """Mask comments and literals; preserve offsets/newlines for source joins."""
 need(type(raw) is bytes and 0<len(raw)<=256*1024,'Public interface source exceeds bound')
 text=raw.decode('utf-8');out=list(text);i=0;n=len(text)
 def blank(start,end):
  for p in range(start,end):
   if text[p] not in '\r\n':out[p]=' '
 while i<n:
  start=i
  if text.startswith('//',i):
   end=text.find('\n',i+2);i=n if end<0 else end;blank(start,i)
  elif text.startswith('/*',i):
   i+=2;depth=1
   while i<n and depth:
    if text.startswith('/*',i):depth+=1;i+=2
    elif text.startswith('*/',i):depth-=1;i+=2
    else:i+=1
   need(depth==0,'Unterminated Kotlin source comment');blank(start,i)
  elif text.startswith('"""',i):
   end=text.find('"""',i+3);need(end>=0,'Unterminated Kotlin source raw string');i=end+3;blank(start,i)
  elif text[i] in ('"',"'"):
   quote=text[i];i+=1;closed=False
   while i<n:
    if text[i]=='\\':i+=2
    elif text[i]==quote:i+=1;closed=True;break
    else:i+=1
   need(closed,'Unterminated Kotlin source literal');blank(start,min(i,n))
  else:i+=1
 return ''.join(out)

def _source_parameter_types(parameters):
 """Split only outer commas; defaults may contain nested calls/generics."""
 if not parameters.strip():return []
 parts=[];start=0;stack=[];pairs={')':'(',']':'[','}':'{','>':'<'}
 for i,char in enumerate(parameters):
  if char in '([{<':stack.append(char)
  elif char in ')]}>':
   need(stack and stack.pop()==pairs[char],'Unbalanced current public source parameter expression')
  elif char==',' and not stack:parts.append(parameters[start:i]);start=i+1
 need(not stack,'Unterminated current public source parameter expression');parts.append(parameters[start:])
 if len(parts)>1 and not parts[-1].strip():parts.pop()
 types=[]
 for part in parts:
  need(':' in part,'Current public source parameter type missing')
  name,value=part.split(':',1);need(re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*',name.strip()) is not None,
       'Current public source parameter name differs')
  types.append(_source_type(value.split('=',1)[0]))
 return types

def _source_type(value):
 need(re.search(r'[A-Za-z0-9_$]\s+[A-Za-z0-9_$]',value) is None,
      'Whitespace inside a public source type identifier')
 value=re.sub(r'\s+','',value)
 need(re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*(?:<[A-Za-z0-9_$<>,?]+>)?\??',value) is not None,
      'Current public logical source type differs')
 depth=0
 for char in value:
  if char=='<':depth+=1
  elif char=='>':depth-=1;need(depth>=0,'Unbalanced current public source generic type')
 need(depth==0,'Unterminated current public source generic type');return value

def extract_api_source(raw):
 """Selected top-level MDArray signatures, preserving ordered nullable types."""
 code=_kotlin_code(raw);declarations=list(re.finditer(r'\binterface\s+MDArray\b[^{}]*\{',code))
 need(len(declarations)==1,'Current MDArray interface declaration differs')
 start=declarations[0].end();depth=1;end=start
 while end<len(code) and depth:
  if code[end]=='{':depth+=1
  elif code[end]=='}':depth-=1
  end+=1
 need(depth==0,'Unterminated current MDArray interface');body=code[start:end-1]
 levels=[];depth=0
 for char in body:
  levels.append(depth)
  if char=='{':depth+=1
  elif char=='}':depth-=1
 names={m['name'] for m in _API_CONTRACTS};found=[]
 pattern=r'(?m)^[ \t]*(?:public[ \t]+)?suspend[ \t]+fun[ \t]+([A-Za-z_$][A-Za-z0-9_$]*)[ \t]*\('
 for match in re.finditer(pattern,body):
  if match[1] not in names or levels[match.start()]!=0:continue
  pos=match.end();parameter_start=pos;depth=1
  while pos<len(body) and depth:
   if body[pos]=='(':depth+=1
   elif body[pos]==')':depth-=1
   pos+=1
  need(depth==0,'Unterminated current public source parameter list')
  types=_source_parameter_types(body[parameter_start:pos-1])
  while pos<len(body) and body[pos].isspace():pos+=1
  need(pos<len(body) and body[pos]==':','Current public source return type missing');pos+=1
  while pos<len(body) and body[pos].isspace():pos+=1
  return_start=pos;token=re.match(r'[A-Za-z_$][A-Za-z0-9_$]*',body[pos:])
  need(token is not None,'Current public source return type differs');pos+=token.end()
  cursor=pos
  while cursor<len(body) and body[cursor].isspace():cursor+=1
  if cursor<len(body) and body[cursor]=='<':
   pos=cursor+1;depth=1
   while pos<len(body) and depth:
    if body[pos]=='<':depth+=1
    elif body[pos]=='>':depth-=1
    pos+=1
   need(depth==0,'Unterminated current public return generic type')
  cursor=pos
  while cursor<len(body) and body[cursor] in ' \t':cursor+=1
  if cursor<len(body) and body[cursor]=='?':pos=cursor+1
  returns=_source_type(body[return_start:pos]);tail=body[pos:body.find('\n',pos) if '\n' in body[pos:] else len(body)].strip()
  need(not tail or tail.startswith((';','{','=')),'Current public source declaration tail differs')
  found.append({'logicalId':'MDArray.'+match[1]+'('+','.join(types)+')',
                'name':match[1],'parameterTypes':types,'returnType':returns})
 return found

def verify_api_source(methods,raw):
 """JVM erasure cannot prove source nullability or distinguish logical types."""
 validate_api_methods(methods);found={}
 for declaration in extract_api_source(raw):found.setdefault(declaration['logicalId'],[]).append(declaration)
 need(set(found)=={method['logicalId'] for method in methods},
      'Current interface full-signature declaration inventory differs')
 for method in methods:
  expected={key:method[key] for key in ('logicalId','name','parameterTypes','returnType')}
  need(found[method['logicalId']]==[expected],
       'Current interface ordered parameter/nullable return declaration differs: '+method['logicalId'])
 return True

def validate_nc4_contract(nc4,by):
 exact(nc4,('templatePath','entrypoint','assertionFunctions'),'NC4 contract')
 need(type(nc4['templatePath']) is str and nc4['templatePath'] in by,'NC4 template source association differs')
 entry=nc4['entrypoint'];need(type(entry) is str and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',entry),'NC4 entrypoint differs')
 names=nc4['assertionFunctions'];need(type(names) is list and len(names)==7,'NC4 requires an ordered seven-function list')
 seen=set()
 for name in names:
  need(type(name) is str and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',name) and name not in seen and name!=entry,'NC4 assertion function differs')
  seen.add(name)
 return nc4

def validate(document):
 exact(document,_KEYS,'Static intent');need(type(document['schemaVersion']) is int and document['schemaVersion']==1 and document['kind']=='GDAL-cohort-static-intent','Static intent schema differs')
 need(isinstance(document['cohort'],str) and re.fullmatch(r'C[1-9][0-9]{0,3}',document['cohort']) and string(document['feature'],128),'Static cohort/feature label differs')
 s=document['scope'];exact(s,('repository','repositoryId','pr','branch','head','tree','base','baseTree','testCheckout','primaryRun','profileRun'),'Scope')
 need(isinstance(s['repository'],str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',s['repository']) and integer(s['repositoryId']) and integer(s['pr']) and string(s['branch'],256) and not s['branch'].startswith('/') and '..' not in s['branch'],'Static repository/branch identity differs')
 need(all(hexstr(s[k],40) for k in ('head','tree','base','baseTree','testCheckout')) and s['head']!=s['base'] and s['testCheckout'] not in (s['head'],s['base']) and integer(s['primaryRun']) and integer(s['profileRun']) and s['primaryRun']!=s['profileRun'],'Static H/T/B/checkout/run identity differs')
 source=document['source'];exact(source,('root','trackedFileCount','nativeInputCount','portVersion','defaultEngineFingerprint','references','changedFiles','patchSequence'),'Source');absolute(source['root']);by=_source_refs(source['references'])
 need(integer(source['trackedFileCount']) and source['trackedFileCount']==len(by) and integer(source['nativeInputCount']) and source['nativeInputCount']==sum(any(n.startswith(p) for p in _NATIVE) for n in by) and integer(source['portVersion'],0,2**31-1) and hexstr(source['defaultEngineFingerprint'],64),'Static source count/port/fingerprint differs')
 need(type(source['changedFiles']) is list and source['changedFiles'],'Current source diff missing');seen=set()
 for item in source['changedFiles']:
  exact(item,('path','status','gitBlob'),'Changed file');need(item['path'] in by and item['path'] not in seen and item['status'] in ('added','modified') and item['gitBlob']==by[item['path']]['gitBlob'],'Current changed-file identity differs');seen.add(item['path'])
 need(type(source['patchSequence']) is list and source['patchSequence'],'Native patch sequence missing');seen=set()
 for item in source['patchSequence']:
  exact(item,('path','bytes','sha256'),'Native patch');safe_relative(item['path']);need(integer(item['bytes'],0,8*MiB) and hexstr(item['sha256'],64),'Native patch byte identity type differs');need(item['path'] in by and item['path'].startswith('gdal4k-engine/ports/gdal/') and item['path'] not in seen and item=={k:by[item['path']][k] for k in item},'Native patch reference differs');seen.add(item['path'])
 ci=document['ci'];exact(ci,('requiredRoles','maximumAgeSeconds','workflowSourceRefs','publicationRole'),'CI');exact(ci['requiredRoles'],('primary','profile'),'CI role sets');allroles=[]
 for names in ci['requiredRoles'].values():need(type(names) is list and names and all(string(n,256) for n in names),'CI role names missing');allroles+=names
 need(len(set(allroles))==len(allroles) and integer(ci['maximumAgeSeconds'],1,600) and string(ci['publicationRole'],128) and ci['publicationRole'] not in allroles,'CI role inventory/freshness differs')
 need(type(ci['workflowSourceRefs']) is list and ci['workflowSourceRefs'],'Workflow source refs missing')
 for r in ci['workflowSourceRefs']:exact(r,('path','bytes','sha256'),'Workflow reference');safe_relative(r['path']);need(integer(r['bytes'],0,8*MiB) and hexstr(r['sha256'],64),'Workflow reference byte identity type differs');need(r['path'] in by and r['path'].startswith('.github/workflows/') and r=={k:by[r['path']][k] for k in r},'Workflow source reference differs')
 validate_api_methods(document['apiMethods'])
 contracts=document['contracts'];exact(contracts,('common','wire','nc4'),'Contract groups');seen=set()
 for kind in ('common','wire'):
  need(type(contracts[kind]) is list and contracts[kind],'Source test group missing')
  for g in contracts[kind]:
   exact(g,('className','path','methods'),'Test group');need(isinstance(g['className'],str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',g['className']) and g['className'] not in seen and g['path'] in by and g['path'].endswith('/'+g['className']+'.kt') and ('/src/commonTest/' if kind=='common' else '/src/jvmTest/') in g['path'],'Source test class/path identity differs');seen.add(g['className'])
   need(type(g['methods']) is list and g['methods'] and len(set(g['methods']))==len(g['methods']) and all(isinstance(n,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',n) for n in g['methods']),'Source test method inventory differs')
 validate_nc4_contract(contracts['nc4'],by)
 exact(document['packages'],('defaultCoordinates','jvmSliceCoordinates','profileFamilies','jvmClassifiers'),'Package expectations');need(all(integer(v) for v in document['packages'].values()),'Package count type differs')
 names=document['familyNames'];need(type(names) is list and names==sorted(set(names)) and names and all(isinstance(n,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,255}',n) for n in names),'Logical family name inventory differs')
 exact(document['resources'],_RESOURCES,'Resources');need(all(type(document['resources'][k]) is int and document['resources'][k]==v for k,v in _RESOURCES.items()),'Reviewed physical/capture bounds cannot be overridden by intent')
 return document

def verify_current_source(document):
 source=document['source'];root=source['root'];scope=document['scope']
 def git(*args):
  p=subprocess.run(['git','-C',root,*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False);need(p.returncode==0 and len(p.stdout)<=2*MiB,'Current source Git identity command failed/exceeded bound');return p.stdout
 need(git('rev-parse','HEAD','HEAD^{tree}').decode().splitlines()==[scope['head'],scope['tree']],'Intent does not match actual current source HEAD/tree')
 need(git('rev-parse',scope['base']+'^{tree}').decode().strip()==scope['baseTree'],'Intent base tree differs from local committed base')
 observed={}
 for entry in git('ls-tree','-r','-z',scope['head']).split(b'\0'):
  if not entry:continue
  meta,name=entry.split(b'\t',1);mode,kind,blob=meta.decode().split();need(kind=='blob' and mode in ('100644','100755'),'Current source has unsupported tree object');observed[name.decode()]=(mode,blob)
 need(observed=={r['path']:(r['gitMode'],r['gitBlob']) for r in source['references']},'Intent complete Git source map differs')
 raw={}
 for r in source['references']:
  path=str(Path(root)/r['path']);body=read_bytes(path,8*MiB);s=os.lstat(path);need(len(body)==r['bytes'] and hashlib.sha256(body).hexdigest()==r['sha256'] and hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()==r['gitBlob'] and bool(s.st_mode&0o111)==(r['gitMode']=='100755'),'Intent source bytes/Git blob/mode differ: '+r['path']);raw[r['path']]=body
 need(_API_SOURCE in raw,'Current MDArray interface source missing');verify_api_source(document['apiMethods'],raw[_API_SOURCE])
 port=parse_json(raw['gdal4k-engine/ports/gdal/vcpkg.json']);need(port['version-semver']=='3.12.4' and port['port-version']==source['portVersion'],'Current GDAL port version differs')
 listing=raw['gdal4k-engine/ports/gdal/portfile.cmake'].decode().split('    PATCHES\n',1)[1].split('\n)',1)[0].split();need([r['path'] for r in source['patchSequence']]==['gdal4k-engine/ports/gdal/'+n for n in listing],'Current native patch order differs')
 for groups in (document['contracts']['common'],document['contracts']['wire']):
  for g in groups:
   methods=re.findall(r'@Test\s+fun\s+(\w+)\s*\(',raw[g['path']].decode());need(methods==g['methods'],'Current test method names/order differ: '+g['className'])
 version=re.search(rb'(?m)^gdal4kVersion\s*=\s*(\S+)\s*$',raw['gradle.properties']);need(version is not None,'Current binary version missing');digest=hashlib.sha256(version[1])
 for prefix in _NATIVE:
  for name in sorted(n for n in raw if n.startswith(prefix)):digest.update(name.encode());digest.update(raw[name].replace(b'\r\n',b'\n'))
 need(digest.hexdigest()==source['defaultEngineFingerprint'],'Current native source fingerprint differs')
 return True

def install(reference):
 global _CURRENT,_REF
 validate_reference(reference)
 if _CURRENT is not None:need(reference==_REF,'A process cannot switch static cohort intents');return copy.deepcopy(_CURRENT)
 raw=read_pin(reference,sealed=True);document=validate(parse_json(raw));verify_current_source(document);_CURRENT=document;_REF=copy.deepcopy(reference);return copy.deepcopy(document)
def current():
 need(_CURRENT is not None,'Static cohort intent has not been installed');return copy.deepcopy(_CURRENT)
def current_ref():
 need(_REF is not None,'Static cohort intent has not been installed');return copy.deepcopy(_REF)
def initialize_for_process(argv=None):
 if _CURRENT is not None:return current()
 args=list(sys.argv[1:] if argv is None else argv);values={}
 for flag in ('--cohort-manifest','--cohort-manifest-sha256'):
  need(args.count(flag)==1 and args.index(flag)+1<len(args),'Exactly one pinned cohort intent argument is required');values[flag]=args[args.index(flag)+1]
 path=absolute(values['--cohort-manifest']);reference={'path':path,'bytes':os.lstat(path).st_size,'sha256':values['--cohort-manifest-sha256']};return install(reference)
def add_arguments(parser):
 parser.add_argument('--cohort-manifest',required=True,type=Path);parser.add_argument('--cohort-manifest-sha256',required=True)
def require_intent_reference(reference):
 validate_reference(reference)
 need(reference==current_ref(),'Concrete reference differs from the installed static intent')
 return True
def require_plan_intent(plan):
 need(type(plan) is dict,'Concrete plan/config must be an object')
 require_intent_reference(plan.get('cohortManifest'))

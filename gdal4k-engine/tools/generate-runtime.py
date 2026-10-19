#!/usr/bin/env python3
"""Generate checked adapters from the public interfaces. No unsupported-method stubs.

The signatures come from gdal4k. C API mappings and exceptions are explicit
below; unknown signatures fail generation. The generated C++ is compiled on every
engine target, so an API/version mismatch cannot become a published stub.
"""
from pathlib import Path
import re
ROOT = Path(__file__).resolve().parents[2]
TYPES = ['GdalRuntime','Dataset','Driver','Band','SpatialReference','CoordinateTransformation','GCP','Layer','Feature','FeatureDefn','FieldDefn','GeomFieldDefn','Geometry','Group','MDArray','Dimension','FieldDomain','Relationship','Attribute','ColorTable','RasterAttributeTable','FeatureColumnStream']

def methods(cls):
    text = '\n'.join(p.read_text() for p in (ROOT/'gdal4k/src/commonMain/kotlin/dev/gdal4k/runtime').glob('*.kt'))
    body = re.search(r'interface '+cls+r'\s*\{(.*?)\n\}',text,re.S)[1]
    body = re.sub(r'/\*.*?\*/','',body,flags=re.S)
    body = re.sub(r'\s*=\s*\w+\(\)', '', body)
    for m in re.finditer(r'fun (\w+)\((.*?)\)(?:\s*:\s*([^\n]+))?',body,re.S):
        name,raw,ret=m.groups();params=[]
        # Defaults containing parentheses (intArrayOf()) are removed before parsing.
        raw=re.sub(r'\s*=\s*intArrayOf\(\)','',raw)
        for part in raw.split(','):
            part=part.strip()
            if not part:continue
            key,typ=part.split(':',1);typ=typ.split('=')[0].strip();params.append((key.strip(),typ))
        yield name,params,(ret or 'Unit').strip()

def key(name,params): return name+''.join('_'+t.replace('?','').replace('<','_').replace('>','') for _,t in params)

def kt_arg(name,typ):
    if typ.rstrip('?') in TYPES:return f'handle({name})'
    if typ in ['DatasetOpenOptions','DatasetInfoOptions']:return f'encode({name})'
    return f'encode({name})'

def kt_result(typ,expr,parent):
    nullable=typ.endswith('?');t=typ.rstrip('?')
    if t in TYPES:
        out=f'engine.wrap{t}({expr}, {parent})'
        return out if nullable else f'requireNotNull({out})'
    converters={'Int':'int','Long':'content.toLong()','ULong':'content.toULong()','Double':'content.toDouble()','String':'contentOrNull.orEmpty()','Boolean':'boolean'}
    if t in converters:
        out=f'{expr}.jsonPrimitive.{converters[t]}'
    elif t in ['DoubleArray','IntArray','ByteArray','ShortArray','FloatArray','LongArray']:
        k=t[:-5];cv={'Double':'toDouble','Int':'toInt','Byte':'toInt().toByte','Short':'toInt().toShort','Float':'toFloat','Long':'toLong'}[k]
        out=f'{expr}.jsonArray.map {{ it.jsonPrimitive.content.{cv}() }}.to{t}()'
    elif t=='List<String>':out=f'{expr}.jsonArray.map {{ it.jsonPrimitive.content }}'
    elif t=='List<DoubleArray>':out=f'{expr}.jsonArray.map {{ p -> p.jsonArray.map {{ it.jsonPrimitive.content.toDouble() }}.toDoubleArray() }}'
    elif t=='RasterStatistics':out=f'{expr}.jsonArray.let {{ RasterStatistics(it[0].jsonPrimitive.content.toDouble(), it[1].jsonPrimitive.content.toDouble(), it[2].jsonPrimitive.content.toDouble(), it[3].jsonPrimitive.content.toDouble()) }}'
    elif t=='RasterHistogram':out=f'{expr}.jsonObject.let {{ RasterHistogram(it.getValue("minimum").jsonPrimitive.content.toDouble(), it.getValue("maximum").jsonPrimitive.content.toDouble(), it.getValue("counts").jsonArray.map {{ n -> n.jsonPrimitive.content.toLong() }}.toLongArray()) }}'
    elif t=='ColorEntry':out=f'{expr}.jsonArray.let {{ ColorEntry(it[0].jsonPrimitive.int, it[1].jsonPrimitive.int, it[2].jsonPrimitive.int, it[3].jsonPrimitive.int) }}'
    elif t=='MDDataType':out=f'decodeMDType({expr})'
    elif t=='FieldDomainBound':out=f'decodeDomainBound({expr})'
    elif t=='List<CodedFieldValue>':out=f'{expr}.jsonArray.map(::decodeCodedValue)'
    elif t=='List<MDValue>':out=f'{expr}.jsonArray.map(::decodeMDValue)'
    elif t=='GdalFiles':out=f'decodeFiles({expr})'
    elif t=='List<GdalDriver>':out=f'{expr}.jsonArray.map {{ it.jsonObject.let {{ d -> GdalDriver(d.getValue("name").jsonPrimitive.content, d.getValue("longName").jsonPrimitive.content, d.getValue("raster").jsonPrimitive.boolean, d.getValue("vector").jsonPrimitive.boolean) }} }}'
    elif t.startswith('List<') and t[5:-1] in TYPES:out=f'{expr}.jsonArray.map {{ requireNotNull(engine.wrap{t[5:-1]}(it, {parent})) }}'
    elif t=='Unit':out=f'{expr}.let {{ Unit }}'
    else:raise ValueError(('Return',typ))
    return f'{expr}.takeUnless {{ it is JsonNull }}?.let {{ v -> '+out.replace(expr,'v')+' }' if nullable else out

# C API names for ordinary scalar and handle operations.
PREFIX={'GdalRuntime':'','Dataset':'GDAL','Driver':'GDAL','Band':'GDAL','SpatialReference':'OSR','CoordinateTransformation':'OCT','Layer':'OGR_L_','Feature':'OGR_F_','FeatureDefn':'OGR_FD_','FieldDefn':'OGR_Fld_','GeomFieldDefn':'OGR_GFld_','Geometry':'OGR_G_'}
ALIASES={
'Dataset':{'getProjection':'GDALGetProjectionRef','getDriver':'GDALGetDatasetDriver','getLayerCount':'GDALDatasetGetLayerCount','getLayer_Int':'GDALDatasetGetLayer','getLayer_String':'GDALDatasetGetLayerByName','resetReading':'GDALDatasetResetReading','testCapability':'GDALDatasetTestCapability'},
'Driver':{'getShortName':'GDALGetDriverShortName','getLongName':'GDALGetDriverLongName','getHelpTopic':'GDALGetDriverHelpTopic','register':'GDALRegisterDriver','deregister':'GDALDeregisterDriver','delete':'GDALDeleteDataset'},
'Band':{'getXSize':'GDALGetRasterBandXSize','getYSize':'GDALGetRasterBandYSize','getRasterDataType':'GDALGetRasterDataType','getColorInterpretation':'GDALGetRasterColorInterpretation','setColorInterpretation':'GDALSetRasterColorInterpretation','getNoDataValue':'GDALGetRasterNoDataValue','setNoDataValue':'GDALSetRasterNoDataValue','deleteNoDataValue':'GDALDeleteRasterNoDataValue','getUnitType':'GDALGetRasterUnitType','setUnitType':'GDALSetRasterUnitType','getCategoryNames':'GDALGetRasterCategoryNames','setCategoryNames':'GDALSetRasterCategoryNames','getSampleOverview':'GDALGetRasterSampleOverviewEx','flushCache':'GDALFlushRasterCache'},
'SpatialReference':{'cloneSpatialReference':'OSRClone','importFromEPSGA':'OSRImportFromEPSGA','getName':'OSRGetName','hasPointMotionOperation':'OSRHasPointMotionOperation','cloneGeogCS':'OSRCloneGeogCS'},
'CoordinateTransformation':{'getInverse':'OCTGetInverse'},
'Feature':{'getDefinition':'OGR_F_GetDefnRef','cloneFeature':'OGR_F_Clone','getFieldDefn':'OGR_F_GetFieldDefnRef','getGeomFieldDefn':'OGR_F_GetGeomFieldDefnRef','getGeomFieldRef':'OGR_F_GetGeomFieldRef','setGeomField':'OGR_F_SetGeomField','getFieldAsInteger64':'OGR_F_GetFieldAsInteger64'},
'FeatureDefn':{'getFieldDefn':'OGR_FD_GetFieldDefn','getGeomFieldDefn':'OGR_FD_GetGeomFieldDefn'},
'FieldDefn':{'getName':'OGR_Fld_GetNameRef','getAlternativeName':'OGR_Fld_GetAlternativeNameRef','getFieldType':'OGR_Fld_GetType','getTypeName':'OGR_GetFieldTypeName','getDomainName':'OGR_Fld_GetDomainName','getDomainNameRef':'OGR_Fld_GetDomainName','getComment':'OGR_Fld_GetComment','getCommentRef':'OGR_Fld_GetComment'},
'GeomFieldDefn':{'getName':'OGR_GFld_GetNameRef'},
'Geometry':{'cloneGeometry':'OGR_G_Clone','getArea':'OGR_G_Area','geodesicLength':'OGR_G_GeodesicLength','geodesicArea':'OGR_G_GeodesicArea','equal':'OGR_G_Equals','getCoordinateDimension':'OGR_G_GetCoordinateDimension','setCoordinateDimension':'OGR_G_SetCoordinateDimension','getDimension':'OGR_G_GetDimension','wkbSize':'OGR_G_WkbSizeEx','hasCurveGeometry':'OGR_G_HasCurveGeometry'},
}
OWNED={('Driver','create'),('Driver','createCopy'),('Driver','createVector'),('GdalRuntime','openDataset'),('GdalRuntime','createSpatialReference'),('GdalRuntime','createCoordinateTransformation'),('GdalRuntime','createGCP'),('Feature','cloneFeature'),('Layer','getFeature'),('Layer','getNextFeature'),('SpatialReference','cloneSpatialReference'),('SpatialReference','cloneGeogCS'),('SpatialReference','convertToOtherProjection'),('CoordinateTransformation','getInverse')}
for m in ['cloneGeometry','centroid','pointOnSurface','boundary','convexHull','buffer','simplify','simplifyPreserveTopology','makeValid','normalize','getLinearGeometry','getCurveGeometry']:OWNED.add(('Geometry',m))
SPECIAL={}
def special(cls,name,body):SPECIAL[(cls,name)]=body

OWNED.add(('FieldDefn','cloneFieldDefn'))
special('FieldDefn','cloneFieldDefn','auto original=OGRFieldDefn::FromHandle(h);auto copy=new OGRFieldDefn(original);copy->SetIgnored(original->IsIgnored());return r.result(OGRFieldDefn::ToHandle(copy),"FieldDefn",true);')
for method in ['createField','deleteField','reorderFields','reorderField','alterFieldDefn']:
    special('Layer',method,f'return changeLayerFields(r,h,a,"{method}");')

for method in ['createGeometryFromWkt','createGeometryFromWkb','createGeometryFromGeoJson']:
    OWNED.add(('GdalRuntime',method))
    special('GdalRuntime',method,'return createVectorGeometry(r,a,"'+method+'");')
for method in ['intersection','union','difference']:
    OWNED.add(('Geometry',method))
    special('Geometry',method,'return vectorOverlay(r,h,a,"'+method+'");')
OWNED.update({('Geometry','getSpatialReference'),('Layer','getSpatialFilter')})
special('Geometry','getSpatialReference','auto s=OGR_G_GetSpatialReference(h);return r.result(s?OSRClone(s):nullptr,"SpatialReference",true);')
special('Geometry','assignSpatialReference','auto s=a["spatialReference"].GetType()==CPLJSONObject::Type::Null?nullptr:r.arg(a["spatialReference"]);OGR_G_AssignSpatialReference(h,s);return nullValue();')
special('Layer','getSpatialFilter','auto g=OGR_L_GetSpatialFilter(h);return r.result(g?OGR_G_Clone(g):nullptr,"Geometry",true);')
for method in ['setSpatialFilter','setSpatialFilterRect']:
    special('Layer',method,'return setVectorSpatialFilter(r,h,a,'+str(method.endswith('Rect')).lower()+');')

special('GdalRuntime','writeMemoryFile','return writeMemoryFile(a);')
special('GdalRuntime','readMemoryFile','return readMemoryFile(a);')
special('GdalRuntime','deleteMemoryFile','auto path=a["path"].ToString();require(path.rfind("/vsimem/",0)==0,"Memory file path must start with /vsimem/");VSIStatBufL info;if(VSIStatL(path.c_str(),&info)==0){auto code=VSI_ISDIR(info.st_mode)?VSIRmdirRecursive(path.c_str()):VSIUnlink(path.c_str());require(code==0,"Cannot remove memory file");}return nullValue();')

special('GdalRuntime','versionInfo','auto request=a["request"].ToString();return value(request=="THIRD_PARTY_LICENSES"?gdal4k_license_notices():GDALVersionInfo(request.c_str()));')

# Bodies have access to h (receiver), a (named JSON arguments), and engine r.
# Each returns an object with a "value" member.
special('GdalRuntime','getDriver','return r.result(GDALGetDriverByName(a["name"].ToString().c_str()), "Driver", false);')
special('GdalRuntime','createSpatialReference','auto s=OSRNewSpatialReference(nullptr); if(a["definition"].IsValid() && OSRSetFromUserInput(s,a["definition"].ToString().c_str())!=OGRERR_NONE) { OSRDestroySpatialReference(s); throw std::runtime_error(CPLGetLastErrorMsg()); } return r.result(s,"SpatialReference",true);')
special('GdalRuntime','createCoordinateTransformation','return r.result(OCTNewCoordinateTransformation(r.arg(a["source"]),r.arg(a["target"])),"CoordinateTransformation",true);')
special('GdalRuntime','createGCP','auto p=static_cast<GDAL_GCP*>(CPLCalloc(1,sizeof(GDAL_GCP))); GDALInitGCPs(1,p); p->dfGCPX=a["gcpX"].ToDouble();p->dfGCPY=a["gcpY"].ToDouble();p->dfGCPZ=a["gcpZ"].ToDouble();p->dfGCPPixel=a["gcpPixel"].ToDouble();p->dfGCPLine=a["gcpLine"].ToDouble();CPLFree(p->pszInfo);CPLFree(p->pszId);p->pszInfo=CPLStrdup(a["info"].ToString().c_str());p->pszId=CPLStrdup(a["id"].ToString().c_str());return r.result(p,"GCP",true);')
special('GdalRuntime','openDataset','if(a["files"].IsValid())return openMemoryDataset(r,a);auto opt=a["options"]; auto flags=GDAL_OF_VERBOSE_ERROR | (opt.GetBool("update")?GDAL_OF_UPDATE:GDAL_OF_READONLY) | opt.GetInteger("kind"); Strings list(opt.GetArray("options")); auto ds=GDALOpenEx(a["source"].ToString().c_str(),flags,nullptr,list.data(),nullptr);if(!ds) throw std::runtime_error(CPLGetLastErrorMsg());return r.result(ds,"Dataset",true);')
special('GdalRuntime','datasetInfo','return value(datasetInfo(r.arg(a["dataset"]),a["options"].GetArray("arguments"),false));')
special('GdalRuntime','drivers','return driverList();')
OWNED.add(('GdalRuntime','buildVRT'))
special('GdalRuntime','buildVRT','return buildVirtualDataset(r,a);')
special('GdalRuntime','warp','return warpDatasets(r,a,false);')
special('GdalRuntime','warpTo','return warpDatasets(r,a,true);')
PARENT_OWNED={('Dataset','getRootGroup'),('Group','openGroup'),('Group','openArray'),('Group','createGroup'),('Group','createDimension'),('Group','createArray')}
OWNED.update({('Driver','createMultidimensional'),('GdalRuntime','createGlobFieldDomain'),('GdalRuntime','createRelationship'),('Dataset','getFieldDomain'),('Dataset','getRelationship')})
PREFIX.update({'Group':'GDALGroup','Dimension':'GDALDimension','MDArray':'GDALMDArray','FieldDomain':'OGR_FldDomain_','Relationship':'GDALRelationship'})
special('Driver','createMultidimensional','Strings root(a.GetArray("rootOptions")),opts(a.GetArray("options"));return r.result(GDALCreateMultiDimensional(h,a["name"].ToString().c_str(),root.data(),opts.data()),"Dataset",true);')
special('Dataset','getRootGroup','return r.result(GDALDatasetGetRootGroup(h),"Group",true,id);')
for method,typ in [('getFieldDomain','FieldDomain'),('getRelationship','Relationship')]:
    cast='OGRFieldDomain' if typ=='FieldDomain' else 'GDALRelationship'
    clone='reinterpret_cast<OGRFieldDomain*>(p)->Clone()' if typ=='FieldDomain' else 'new GDALRelationship(*reinterpret_cast<GDALRelationship*>(p))'
    special('Dataset',method,f'auto p=GDALDatasetGet{typ}(h,a["name"].ToString().c_str());return r.result(p?{clone}:nullptr,"{typ}",true);')
for typ,arg in [('FieldDomain','domain'),('Relationship','relationship')]:
    special('Dataset','get'+typ+'Names',f'auto p=GDALDatasetGet{typ}Names(h,nullptr);auto out=strings(p);CSLDestroy(p);return out;')
    for op in ['add','update','delete']:
        operand='a["name"].ToString().c_str()' if op=='delete' else f'static_cast<OGRFieldDomainH>(r.arg(a["{arg}"]))' if typ=='FieldDomain' else f'r.arg(a["{arg}"])'
        guard='validateDomainStorage(h,static_cast<OGRFieldDomainH>(r.arg(a["domain"])));' if typ=='FieldDomain' and op!='delete' else ''
        special('Dataset',op+typ,f'{guard}char* reason=nullptr;auto ok=GDALDataset{op.capitalize()}{typ}(h,{operand},&reason);std::string message=reason?reason:"GDAL {op} {typ} failed";CPLFree(reason);if(!ok)throw std::runtime_error(message);return nullValue();')
special('GdalRuntime','createGlobFieldDomain','return r.result(OGR_GlobFldDomain_Create(a["name"].ToString().c_str(),a["description"].ToString().c_str(),static_cast<OGRFieldType>(a.GetInteger("fieldType")),static_cast<OGRFieldSubType>(a.GetInteger("fieldSubType")),a["glob"].ToString().c_str()),"FieldDomain",true);')
special('GdalRuntime','createRelationship','auto c=a.GetInteger("cardinality");require(c>=0&&c<=3,"Invalid relationship cardinality");return r.result(GDALRelationshipCreate(a["name"].ToString().c_str(),a["leftTable"].ToString().c_str(),a["rightTable"].ToString().c_str(),static_cast<GDALRelationshipCardinality>(c)),"Relationship",true);')
special('FieldDomain','getGlob','return value(OGR_FldDomain_GetDomainType(h)==OFDT_GLOB?OGR_GlobFldDomain_GetGlob(h):nullptr);')
for method in ['getLeftTableFields','getRightTableFields']:
    special('Relationship',method,'auto p=GDALRelationship'+method[0].upper()+method[1:]+'(h);auto out=strings(p);CSLDestroy(p);return out;')
for method,fn in [('getGroupNames','GDALGroupGetGroupNames'),('getArrayNames','GDALGroupGetMDArrayNames')]:
    special('Group',method,f'Strings opts(a.GetArray("options"));auto p={fn}(h,opts.data());auto out=strings(p);CSLDestroy(p);return out;')
for method,fn,typ in [('openGroup','GDALGroupOpenGroup','Group'),('openArray','GDALGroupOpenMDArray','MDArray'),('createGroup','GDALGroupCreateGroup','Group')]:
    special('Group',method,f'Strings opts(a.GetArray("options"));return r.result({fn}(h,a["name"].ToString().c_str(),opts.data()),"{typ}",true,id);')
special('Group','createDimension','auto size=a.GetLong("size");require(size>0,"Dimension size must be positive");Strings opts(a.GetArray("options"));return r.result(GDALGroupCreateDimension(h,a["name"].ToString().c_str(),a["type"].ToString().c_str(),a["direction"].ToString().c_str(),size,opts.data()),"Dimension",true,id);')
special('Group','createArray','std::vector<GDALDimensionH> dims;for(auto d:a.GetArray("dimensions"))dims.push_back(static_cast<GDALDimensionH>(r.arg(d)));auto type=static_cast<GDALDataType>(a.GetInteger("dataType"));require(type>GDT_Unknown&&type<GDT_TypeCount,"Invalid numeric data type");auto edt=GDALExtendedDataTypeCreate(type);Strings opts(a.GetArray("options"));auto array=GDALGroupCreateMDArray(h,a["name"].ToString().c_str(),dims.size(),dims.data(),edt,opts.data());GDALExtendedDataTypeRelease(edt);return r.result(array,"MDArray",true,id);')
special('Dimension','getSize','auto size=GDALDimensionGetSize(h);require(size<=INT64_MAX,"Dimension size exceeds Kotlin Long");return value(std::to_string(size));')
special('MDArray','getDimensions','size_t n=0;auto dims=GDALMDArrayGetDimensions(h,&n);CPLJSONArray out;for(size_t i=0;i<n;++i)out.Add(r.result(dims[i],"Dimension",true,id)["value"]);CPLFree(dims);return value(out);')
special('MDArray','getDataType','auto type=GDALMDArrayGetDataType(h);auto numeric=GDALExtendedDataTypeGetClass(type)==GEDTC_NUMERIC?GDALExtendedDataTypeGetNumericDataType(type):GDT_Unknown;GDALExtendedDataTypeRelease(type);return value(numeric);')
special('MDArray','read','return mdRaster(h,a,false);')
special('MDArray','write','return mdRaster(h,a,true);')

# Extended multidimensional values keep all ABI layout and dynamic allocations inside the engine.
PREFIX['Attribute']='GDALAttribute'
OWNED.update({('GdalRuntime','createCodedFieldDomain'),('GdalRuntime','createRangeFieldDomain')})
special('Group','createArray_String_List_Dimension_MDDataType_List_String','return createExtendedArray(r,h,a,id);')
special('MDArray','getExtendedDataType','return extendedTypeResult(GDALMDArrayGetDataType(h));')
for op in ['readValues','writeValues']:
    special('MDArray',op,'return mdValues(h,a,'+str(op=='writeValues').lower()+');')
for owner in ['Group','MDArray']:
    for op in ['getAttribute','getAttributes','createAttribute','deleteAttribute']:
        PARENT_OWNED.add((owner,op))
        special(owner,op,f'return attributeOperation(r,h,a,id,"{owner}","{op}");')
special('Attribute','getDataType','return extendedTypeResult(GDALAttributeGetDataType(h));')
special('Attribute','getDimensions','size_t n=0;auto p=GDALAttributeGetDimensionsSize(h,&n);auto out=numbers(p,n);CPLFree(p);return out;')
special('Attribute','readValues','return attributeValues(h,a,false);')
special('Attribute','writeValues','return attributeValues(h,a,true);')
special('GdalRuntime','createCodedFieldDomain','return createCodedDomain(r,a);')
special('GdalRuntime','createRangeFieldDomain','return createRangeDomain(r,a);')
special('FieldDomain','getCodedValues','return codedDomainValues(h);')
special('FieldDomain','getMinimum','return rangeDomainBound(h,true);')
special('FieldDomain','getMaximum','return rangeDomainBound(h,false);')

OWNED.update({('GdalRuntime','createFeature'),('GdalRuntime','createFieldDefn')})
special('GdalRuntime','createFeature','return r.result(OGR_F_Create(r.arg(a["definition"])),"Feature",true);')
special('GdalRuntime','createFieldDefn','return r.result(OGR_Fld_Create(a["name"].ToString().c_str(),static_cast<OGRFieldType>(a.GetInteger("type"))),"FieldDefn",true);')
# Dataset additions use the same checked transport on every target.
for method,fn in [('getDescription','GDALGetDescription'),('setDescription','GDALSetDescription'),('getAccess','GDALGetAccess'),('createMaskBand','GDALCreateDatasetMaskBand'),('clearStatistics','GDALDatasetClearStatistics'),('startTransaction','GDALDatasetStartTransaction'),('commitTransaction','GDALDatasetCommitTransaction'),('rollbackTransaction','GDALDatasetRollbackTransaction')]:
    ALIASES['Dataset'][method]=fn
for method,fn in [('getMetadata','GDALGetMetadata'),('getMetadataDomainList','GDALGetMetadataDomainList')]:
    extra=',a["domain"].ToString().c_str()' if method=='getMetadata' else ''
    cleanup='CSLDestroy(p);' if method=='getMetadataDomainList' else ''
    special('Dataset',method,f'auto p={fn}(h{extra});auto out=strings(p);{cleanup}return out;')
special('Dataset','setMetadata','Strings list(a.GetArray("metadata"));return value(GDALSetMetadata(h,list.data(),a["domain"].ToString().c_str()));')
special('Dataset','getMetadataItem','return value(GDALGetMetadataItem(h,a["name"].ToString().c_str(),a["domain"].ToString().c_str()));')
special('Dataset','setMetadataItem','auto text=a["value"].ToString();return value(GDALSetMetadataItem(h,a["name"].ToString().c_str(),a["value"].GetType()==CPLJSONObject::Type::Null?nullptr:text.c_str(),a["domain"].ToString().c_str()));')
special('Dataset','getSpatialRef','auto s=GDALGetSpatialRef(h);return r.result(s?OSRClone(s):nullptr,"SpatialReference",true);')
OWNED.add(('Dataset','getSpatialRef'))
special('Dataset','setSpatialRef','return value(GDALSetSpatialRef(h,a["spatialReference"].GetType()==CPLJSONObject::Type::Null?nullptr:r.arg(a["spatialReference"])));')
special('Dataset','addBand','Strings list(a.GetArray("options"));return value(GDALAddBand(h,static_cast<GDALDataType>(a.GetInteger("dataType")),list.data()));')
for method,operation in [('translateTo','translate'),('warpTo','warp'),('translateVectorTo','vector')]:
    special('Dataset',method,f'convertDatasetTo(h,a,"{operation}");return nullValue();')
for method in ['readRaster','writeRaster']:
    special('Dataset',method,'return datasetRaster(h,a,method.rfind("writeRaster",0)==0,method);')
special('Dataset','createLayer','Strings list(a.GetArray("options"));auto s=a["spatialReference"];return r.result(GDALDatasetCreateLayer(h,a["name"].ToString().c_str(),s.GetType()==CPLJSONObject::Type::Null?nullptr:r.arg(s),static_cast<OGRwkbGeometryType>(a.GetInteger("geometryType")),list.data()),"Layer",false,id);')
special('Dataset','copyLayer','Strings list(a.GetArray("options"));return r.result(GDALDatasetCopyLayer(h,r.arg(a["source"]),a["name"].ToString().c_str(),list.data()),"Layer",false,id);')
special('Dataset','deleteLayer','auto index=a.GetInteger("index");require(index>=0 && index<GDALDatasetGetLayerCount(h),"Layer index is outside dataset");auto layer=GDALDatasetGetLayer(h,index);r.releaseResultSets(h);auto code=GDALDatasetDeleteLayer(h,index);if(code==OGRERR_NONE)r.invalidatePointer(layer);return value(code);')
special('Dataset','executeSQL','auto filter=a["spatialFilter"];auto dialect=a["dialect"].ToString();auto layer=GDALDatasetExecuteSQL(h,a["statement"].ToString().c_str(),filter.GetType()==CPLJSONObject::Type::Null?nullptr:r.arg(filter),dialect.empty()?nullptr:dialect.c_str());if(!layer && CPLGetLastErrorType()>=CE_Failure)throw std::runtime_error(CPLGetLastErrorMsg());return r.result(layer,"Layer",true,id);')

special('Dataset','info','return value(datasetInfo(h,a.GetArray("arguments"),true));')
special('Dataset','translate','return convertDataset(h,a,"translate");')
special('Dataset','warp','return convertDataset(h,a,"warp");')
special('Dataset','translateVector','return convertDataset(h,a,"vector");')
special('Dataset','demProcessing','return demProcessing(h,a,false);')
special('Dataset','demProcessingTo','return demProcessing(h,a,true);')

special('Dataset','rasterize','return rasterizeDataset(h,a,false);')
special('Dataset','rasterizeTo','return rasterizeDataset(h,a,true);')
special('Dataset','polygonize','return polygonizeDataset(r,h,a,false);')
special('Dataset','polygonizeTo','return polygonizeDataset(r,h,a,true);')
special('Dataset','getDriverShortName','return value(GDALGetDriverShortName(GDALGetDatasetDriver(h)));')
special('Dataset','getDriverLongName','return value(GDALGetDriverLongName(GDALGetDatasetDriver(h)));')
special('Dataset','getGeoTransform','double out[6]; if(GDALGetGeoTransform(h,out)!=CE_None)return nullValue();return numbers(out,6);')
special('Dataset','setGeoTransform','auto v=doubles(a.GetArray("geoTransform")); require(v.size()==6,"GeoTransform must have six elements");return value(GDALSetGeoTransform(h,v.data()));')
special('Dataset','getExtent','OGREnvelope e; if(GDALDataset::FromHandle(h)->GetExtent(&e)!=OGRERR_NONE)return nullValue(); return numbers({e.MinX,e.MaxX,e.MinY,e.MaxY});')
special('Dataset','getFileList','auto p=GDALGetFileList(h);auto out=strings(p);CSLDestroy(p);return out;')
special('Dataset','buildOverviews','auto v=ints(a.GetArray("overviewBands"));return value(GDALBuildOverviews(h,a["resampling"].ToString().c_str(),static_cast<int>(v.size()),v.data(),0,nullptr,progress,activeProgress));')
special('Dataset','getGCPs','CPLJSONArray out;auto p=GDALGetGCPs(h);for(int i=0;i<GDALGetGCPCount(h);i++){auto q=GDALDuplicateGCPs(1,p+i);out.Add(r.result(q,"GCP",true)["value"]);}return value(out);')
special('Dataset','setGCPs','std::vector<GDAL_GCP> v; for(auto p:a.GetArray("gcps"))v.push_back(*static_cast<GDAL_GCP*>(r.arg(p)));if(a["projection"].IsValid())return value(GDALSetGCPs(h,static_cast<int>(v.size()),v.data(),a["projection"].ToString().c_str()));return value(GDALSetGCPs2(h,static_cast<int>(v.size()),v.data(),r.arg(a["spatialReference"])));')
special('Driver','create','Strings list(a.GetArray("options"));return r.result(GDALCreate(h,a["name"].ToString().c_str(),a.GetInteger("xSize"),a.GetInteger("ySize"),a.GetInteger("bandCount"),static_cast<GDALDataType>(a.GetInteger("dataType")),list.data()),"Dataset",true);')
special('Driver','createVector','Strings list(a.GetArray("options"));return r.result(GDALCreate(h,a["name"].ToString().c_str(),0,0,0,GDT_Unknown,list.data()),"Dataset",true);')
special('Driver','createCopy','Strings list(a.GetArray("options"));return r.result(GDALCreateCopy(h,a["name"].ToString().c_str(),r.arg(a["dataset"]),a.GetInteger("strict"),list.data(),progress,activeProgress),"Dataset",true);')
for method,fn in [('rename','GDALRenameDataset'),('copyFiles','GDALCopyDatasetFiles')]:special('Driver',method,f'return value({fn}(h,a["to"].ToString().c_str(),a["from"].ToString().c_str()));')
special('Driver','hasOpenOption','return value(GDALDriverHasOpenOption(h,a["option"].ToString().c_str())!=0);')
# Band metadata uses GDALMajorObject operations just like Dataset.
for method in ['getMetadata', 'getMetadataDomainList', 'setMetadata', 'getMetadataItem', 'setMetadataItem']:
    SPECIAL['Band',method] = SPECIAL['Dataset',method]
ALIASES['Band'].update({'getBandNumber':'GDALGetBandNumber','getAccess':'GDALGetRasterAccess','getDescription':'GDALGetDescription','setDescription':'GDALSetDescription','setScale':'GDALSetRasterScale','setOffset':'GDALSetRasterOffset','fill':'GDALFillRaster'})
for method,fn in [('getScale','GDALGetRasterScale'),('getOffset','GDALGetRasterOffset'),('getMinimum','GDALGetRasterMinimum'),('getMaximum','GDALGetRasterMaximum')]:
    special('Band',method,f'int ok=0;auto v={fn}(h,&ok);return ok?value(v):nullValue();')
for name,typ,fn in [('Int64','GDT_Int64','Int64'),('UInt64','GDT_UInt64','UInt64')]:
    special('Band','getNoDataValueAs'+name,f'require(GDALGetRasterDataType(h)=={typ},"Exact NoData type does not match band");int ok=0;auto v=GDALGetRasterNoDataValueAs{fn}(h,&ok);return ok?value(v):nullValue();')
    parse='std::stoll' if name=='Int64' else 'std::stoull'
    special('Band','setNoDataValueAs'+name,f'require(GDALGetRasterDataType(h)=={typ},"Exact NoData type does not match band");return value(GDALSetRasterNoDataValueAs{fn}(h,{parse}(a["value"].ToString())));')
special('Band','getStatistics','return bandStatistics(h,a,false);')
special('Band','computeStatistics','return bandStatistics(h,a,true);')
special('Band','setStatistics','return setBandStatistics(h,a);')
special('Band','computeRasterMinMax','double v[2];require(GDALComputeRasterMinMax(h,a.GetBool("approximate"),v)==CE_None,"Cannot compute band extrema");return numbers(v,2);')
special('Band','getHistogram','return bandHistogram(h,a);')
special('Band','getDefaultHistogram','return defaultBandHistogram(h,a.GetBool("force"));')
special('Band','setDefaultHistogram','return setBandHistogram(h,a);')
special('Band','getBlockSize','int x,y;GDALGetBlockSize(h,&x,&y);CPLJSONArray out;out.Add(x);out.Add(y);return value(out);')
special('Band','checksum','return value(GDALChecksumImage(h,0,0,GDALGetRasterBandXSize(h),GDALGetRasterBandYSize(h)));')
special('Band','getNoDataValue','int ok;auto v=GDALGetRasterNoDataValue(h,&ok);return ok?value(v):nullValue();')
special('Band','getCategoryNames','return strings(GDALGetRasterCategoryNames(h));')
special('Band','setCategoryNames','Strings list(a.GetArray("names"));return value(GDALSetRasterCategoryNames(h,list.data()));')
for method in ['readRaster','writeRaster']:special('Band',method,'return raster(h,a,method.find("writeRaster")==0,method);')
for axis in ['X','Y','Z','Pixel','Line']:
    special('GCP','getGCP'+axis,f'return value(static_cast<GDAL_GCP*>(h)->dfGCP{axis});')
    special('GCP','setGCP'+axis,f'static_cast<GDAL_GCP*>(h)->dfGCP{axis}=a["value"].ToDouble();return nullValue();')
for field,member in [('Info','pszInfo'),('Id','pszId')]:
    special('GCP','get'+field,f'return value(static_cast<GDAL_GCP*>(h)->{member});')
    special('GCP','set'+field,f'auto p=static_cast<GDAL_GCP*>(h);CPLFree(p->{member});p->{member}=CPLStrdup(a["{field.lower()}"].ToString().c_str());return nullValue();')
for method,fn in [('exportToWkt','OSRExportToWkt'),('exportToPrettyWkt','OSRExportToPrettyWkt'),('exportToProj4','OSRExportToProj4'),('exportToProjJson','OSRExportToPROJJSON'),('exportToXml','OSRExportToXML'),('exportToMICoordSys','OSRExportToMICoordSys')]:
    extra=',false' if method=='exportToPrettyWkt' else ',nullptr' if method=='exportToProjJson' else ',a["rootElement"].ToString().c_str()' if method=='exportToXml' else ''
    special('SpatialReference',method,f'char* out=nullptr;auto err={fn}(h,&out{extra});if(err!=OGRERR_NONE){{CPLFree(out);throw std::runtime_error(CPLGetLastErrorMsg());}}return allocated(out);')
special('SpatialReference','importFromWkt','auto s=a["wkt"].ToString();char* p=s.data();return value(OSRImportFromWkt(h,&p));')
for method,fn in [('getProjParm','OSRGetProjParm'),('getNormProjParm','OSRGetNormProjParm')]:special('SpatialReference',method,f'return value({fn}(h,a["name"].ToString().c_str(),a["defaultValue"].ToDouble(),nullptr));')
for kind in ['Angular','Linear']:
    special('SpatialReference','get'+kind+'Units',f'return value(OSRGet{kind}Units(h,nullptr));')
    special('SpatialReference','get'+kind+'UnitsName',f'char* name=nullptr;OSRGet{kind}Units(h,&name);return value(name);')
special('SpatialReference','getUTMZone','int north;auto z=OSRGetUTMZone(h,&north);return value(north?z:-z);')
special('SpatialReference','getAxisName','OGRAxisOrientation o=OAO_Other;return value(OSRGetAxis(h,a["targetKey"].ToString().c_str(),a.GetInteger("index"),&o));')
special('SpatialReference','getAxisOrientation','OGRAxisOrientation o=OAO_Other;OSRGetAxis(h,a["targetKey"].ToString().c_str(),a.GetInteger("index"),&o);return value(o);')
special('SpatialReference','convertToOtherProjection','return r.result(OSRConvertToOtherProjection(h,a["projection"].ToString().c_str(),nullptr),"SpatialReference",true);')
for name,fn in [('promoteTo3D','OSRPromoteTo3D'),('demoteTo2D','OSRDemoteTo2D')]:special('SpatialReference',name,f'return value({fn}(h,nullptr));')
special('CoordinateTransformation','transformPoint','double x=a["x"].ToDouble(),y=a["y"].ToDouble(),z=a["z"].ToDouble();require(OCTTransform(h,1,&x,&y,&z)!=0,"Coordinate transformation failed");return numbers({x,y,z});')
special('CoordinateTransformation','transformPoints','return transformCoordinatePoints(static_cast<OGRCoordinateTransformationH>(h),a);')
special('CoordinateTransformation','transformBounds','double x0,y0,x1,y1;require(OCTTransformBounds(h,a["xMin"].ToDouble(),a["yMin"].ToDouble(),a["xMax"].ToDouble(),a["yMax"].ToDouble(),&x0,&y0,&x1,&y1,a.GetInteger("densifyPoints"))!=0,"Bounds transformation failed");return numbers({x0,y0,x1,y1});')
special('Layer','getExtent','OGREnvelope e;if(OGR_L_GetExtent(h,&e,a.GetBool("force"))!=OGRERR_NONE)return nullValue();return numbers({e.MinX,e.MaxX,e.MinY,e.MaxY});')
special('Feature','getFieldAsStringList','return strings(OGR_F_GetFieldAsStringList(h,a.GetInteger("index")));')
for name,fn,typ in [('getFieldAsIntegerList','OGR_F_GetFieldAsIntegerList','int'),('getFieldAsDoubleList','OGR_F_GetFieldAsDoubleList','double'),('getFieldAsBinary','OGR_F_GetFieldAsBinary','GByte')]:special('Feature',name,f'int n=0;auto p={fn}(h,a.GetInteger("index"),&n);return numbers(p,n);')
special('Feature','fillUnsetWithDefault','OGR_F_FillUnsetWithDefault(h,false,nullptr);return nullValue();')
special('Feature','dumpReadableAsString','return allocated(OGR_F_DumpReadableAsString(h,nullptr));')
special('FieldDefn','getTypeName','return value(OGR_GetFieldTypeName(OGR_Fld_GetType(h)));')
special('FieldDefn','getFieldTypeName','return value(OGR_GetFieldTypeName(static_cast<OGRFieldType>(a.GetInteger("type"))));')
# Geometry functions with output parameters or options.
for name,fn in [('exportToWkt','OGR_G_ExportToWkt'),('exportToIsoWkt','OGR_G_ExportToIsoWkt')]:special('Geometry',name,f'char* p=nullptr;auto err={fn}(h,&p);if(err!=OGRERR_NONE){{CPLFree(p);throw std::runtime_error(CPLGetLastErrorMsg());}}return allocated(p);')
for name,fn in [('exportToGml','OGR_G_ExportToGML'),('exportToKml','OGR_G_ExportToKML'),('exportToJson','OGR_G_ExportToJson')]:special('Geometry',name,f'return allocated({fn}(h'+(',nullptr' if name=='exportToKml' else '')+'));')
for name,fn in [('exportToWkb','OGR_G_ExportToWkb'),('exportToIsoWkb','OGR_G_ExportToIsoWkb')]:special('Geometry',name,f'std::vector<unsigned char> out(OGR_G_WkbSizeEx(h));require({fn}(h,wkbNDR,out.data())==OGRERR_NONE,"WKB export failed");return numbers(out.data(),out.size());')
special('Geometry','getPoint','double x,y,z;OGR_G_GetPoint(h,a.GetInteger("index"),&x,&y,&z);return numbers({x,y,z});')
special('Geometry','getPoints','CPLJSONArray out;for(int i=0;i<OGR_G_GetPointCount(h);i++){double x,y,z;OGR_G_GetPoint(h,i,&x,&y,&z);out.Add(numbers({x,y,z})["value"]);}return value(out);')
special('Geometry','getEnvelope','OGREnvelope e;OGR_G_GetEnvelope(h,&e);return numbers({e.MinX,e.MaxX,e.MinY,e.MaxY});')
special('Geometry','getEnvelope3D','OGREnvelope3D e;OGR_G_GetEnvelope3D(h,&e);return numbers({e.MinX,e.MaxX,e.MinY,e.MaxY,e.MinZ,e.MaxZ});')
special('Geometry','centroid','auto p=OGR_G_CreateGeometry(wkbPoint);if(OGR_G_Centroid(h,p)!=OGRERR_NONE){OGR_G_DestroyGeometry(p);return nullValue();}return r.result(p,"Geometry",true);')
special('Geometry','buffer','if(a["options"].IsValid()){Strings opt(a.GetArray("options"));return r.result(OGR_G_BufferEx(h,a["distance"].ToDouble(),opt.data()),"Geometry",true);}return r.result(OGR_G_Buffer(h,a["distance"].ToDouble(),30),"Geometry",true);')
special('Geometry','getLinearGeometry','return r.result(OGR_G_GetLinearGeometry(h,0,nullptr),"Geometry",true);')
special('Geometry','getCurveGeometry','return r.result(OGR_G_GetCurveGeometry(h,nullptr),"Geometry",true);')
special('Geometry','hasCurveGeometry','return value(OGR_G_HasCurveGeometry(h,false)!=0);')
special('Geometry','removeGeometry','return value(OGR_G_RemoveGeometry(h,a.GetInteger("index"),true));')
special('Geometry','intersectsExact','return value(OGR_G_Intersects(h,r.arg(a["other"]))!=0);')
for name,fn in [('geodesicLength','OGR_G_GeodesicLength'),('geodesicArea','OGR_G_GeodesicArea')]:special('Geometry',name,f'return value({fn}(h));')

# Band structures: owned palette snapshots, borrowed RAT windows, and native block bytes.
PREFIX.update({'ColorTable':'GDAL','RasterAttributeTable':'GDALRAT'})
OWNED.update({('GdalRuntime','createColorTable'),('GdalRuntime','createRasterAttributeTable'),('ColorTable','cloneColorTable'),('Band','getColorTable'),('RasterAttributeTable','cloneTable'),('RasterAttributeTable','translateToColorTable')})
special('GdalRuntime','createColorTable','auto p=a.GetInteger("paletteInterpretation");require(p>=0&&p<=3,"Invalid palette interpretation");return r.result(GDALCreateColorTable(static_cast<GDALPaletteInterp>(p)),"ColorTable",true);')
special('GdalRuntime','createRasterAttributeTable','return r.result(GDALCreateRasterAttributeTable(),"RasterAttributeTable",true);')
special('Band','getColorTable','auto p=GDALGetRasterColorTable(h);return r.result(p?GDALCloneColorTable(p):nullptr,"ColorTable",true);')
special('Band','setColorTable','return value(GDALSetRasterColorTable(h,a["table"].GetType()==CPLJSONObject::Type::Null?nullptr:r.arg(a["table"])));')
special('Band','getDefaultRAT','return r.result(GDALGetDefaultRAT(h),"RasterAttributeTable",false,id);')
special('Band','setDefaultRAT','auto p=a["table"].GetType()==CPLJSONObject::Type::Null?nullptr:r.arg(a["table"]);auto copy=p?GDALRATClone(p):nullptr;require(!p||copy,"Cannot clone RAT");auto code=GDALSetDefaultRAT(h,copy);GDALDestroyRasterAttributeTable(copy);if(code==CE_None)r.invalidateChildren(h,"RasterAttributeTable");return value(code);')
special('Band','getActualBlockSize','BandBlock b(h,a);int v[2]={b.validWidth,b.validHeight};return numbers(v,2);')
special('Band','getSuggestedBlockAccessPattern','return value(GDALRasterBand::FromHandle(h)->GetSuggestedBlockAccessPattern());')
for op in ['readBlock','writeBlock']:special('Band',op,'return bandBlock(h,a,'+str(op=='writeBlock').lower()+');')
for op in ['getColorEntry','getColorEntryAsRGB','setColorEntry','createColorRamp']:special('ColorTable',op,'return colorTableOperation(h,a,"'+op+'");')
special('RasterAttributeTable','cloneTable','return r.result(GDALRATClone(h),"RasterAttributeTable",true);')
for op in ['getNameOfCol','getTypeOfCol','getUsageOfCol']:special('RasterAttributeTable',op,'auto c=a.GetInteger("column");ratColumn(h,c);return value(GDALRAT'+op[0].upper()+op[1:]+'(h,c));')
for op in ['getValueAsInt','getValueAsDouble','getValueAsString','setValueAsInt','setValueAsDouble','setValueAsString']:special('RasterAttributeTable',op,'return ratScalar(h,a,"'+op+'");')
for op in ['readIntegerValues','writeIntegerValues','readDoubleValues','writeDoubleValues','readStringValues','writeStringValues']:special('RasterAttributeTable',op,'return ratValues(h,a,"'+op+'");')
special('RasterAttributeTable','setRowCount','auto n=a.GetInteger("count");require(n>=0,"Negative RAT row count");GDALRATSetRowCount(h,n);return nullValue();')
special('RasterAttributeTable','createColumn','auto type=a.GetInteger("type"),usage=a.GetInteger("usage");require(type>=0&&type<=2&&usage>=0&&usage<=17,"Invalid RAT column type or usage");auto name=bandString(a["name"]);return value(GDALRATCreateColumn(h,name.c_str(),static_cast<GDALRATFieldType>(type),static_cast<GDALRATFieldUsage>(usage)));')
special('RasterAttributeTable','getColOfUsage','auto u=a.GetInteger("usage");require(u>=0&&u<=17,"Invalid RAT usage");return value(GDALRATGetColOfUsage(h,static_cast<GDALRATFieldUsage>(u)));')
special('RasterAttributeTable','getLinearBinning','double lo=0,step=0;if(!GDALRATGetLinearBinning(h,&lo,&step))return nullValue();return numbers({lo,step});')
special('RasterAttributeTable','setLinearBinning','auto lo=CPLAtof(a["rowZeroMinimum"].ToString().c_str()),step=CPLAtof(a["binSize"].ToString().c_str());require(std::isfinite(lo)&&std::isfinite(step)&&step>0,"Invalid RAT binning");return value(GDALRATSetLinearBinning(h,lo,step));')
special('RasterAttributeTable','getRowOfValue','auto v=CPLAtof(a["value"].ToString().c_str());require(std::isfinite(v),"RAT lookup value must be finite");double lo=0,step=0;if(GDALRATGetLinearBinning(h,&lo,&step)){require(std::isfinite(lo)&&std::isfinite(step)&&step>0,"Invalid RAT binning");auto bin=std::floor((v-lo)/step);if(!std::isfinite(bin)||bin<0||bin>=GDALRATGetRowCount(h))return value(-1);}return value(GDALRATGetRowOfValue(h,v));')
special('RasterAttributeTable','setTableType','auto t=a.GetInteger("type");require(t==0||t==1,"Invalid RAT table type");return value(GDALRATSetTableType(h,static_cast<GDALRATTableType>(t)));')
special('RasterAttributeTable','translateToColorTable','auto n=a.GetInteger("entryCount");require(n==-1||n>0,"Invalid color entry count");return r.result(GDALRATTranslateToColorTable(h,n),"ColorTable",true);')

special('Layer','overlayTo','return layerOverlay(r,h,a);')
special('Layer','getNextFeature','CPLErrorReset();auto p=OGR_L_GetNextFeature(h);fileEnsure(p||CPLGetLastErrorType()<CE_Failure,"Cannot read next feature");return r.result(p,"Feature",true);')
PARENT_OWNED.add(('Layer','openColumnStream'))
special('Layer','openColumnStream','return openColumnStream(r,h,a,id);')
special('FeatureColumnStream','read','try { return readColumnBatch(*static_cast<VectorColumnStream*>(h)); } catch (...) { r.close(id); throw; }')


for op in ['grid','viewshed']:
    special('Dataset',op,'return gridViewshedDataset(h,a,"'+op+'",false);')
    special('Dataset',op+'To','return gridViewshedDataset(h,a,"'+op+'",true);')

for op in ['sieveTo','proximityTo','fillNoData']:
    special('Band',op,'return bandAnalysis(r,h,a,"'+op+'");')
special('Dataset','contour','return contourDataset(h,a,false);')
special('Dataset','contourTo','return contourDataset(h,a,true);')

for operation in ['setNoDataValue','setNoDataValueAsInt64','setNoDataValueAsUInt64','deleteNoDataValue']:
    special('Band',operation,'return setBandNoData(r,h,a,"'+operation+'");')

cpp=[];kt=['// Generated by tools/generate-runtime.py. Edit the mapping, not this file.','package dev.gdal4k.internal','','import dev.gdal4k.runtime.*','import dev.gdal4k.*','import kotlinx.serialization.json.*','']
for cls in TYPES:
    base='RuntimeHandle(engine, id, parent)' if cls!='GdalRuntime' else 'RuntimeHandle(engine, "0", null)'
    args='engine: RuntimeEngine, id: String, parent: RuntimeHandle?' if cls!='GdalRuntime' else 'engine: RuntimeEngine'
    kt.append(f'internal class Bundled{cls}({args}) : {base}, {cls} {{')
    for name,params,ret in methods(cls):
        sig=key(name,params);argstr=', '.join(f'{n}: {t}' for n,t in params)
        if name=='close':kt.append('    override suspend fun close() = closeHandle()');continue
        payload=', '.join('"bufferSize" to encode(buffer.size)' if cls in ['Band','Dataset'] and name in ['readRaster','readBlock'] and n=='buffer'
                         else '"'+n+'" to '+kt_arg(n,t) for n,t in params)
        call=f'invoke("{cls}", "{sig}", mapOf({payload}))'
        if cls in ['Band','Dataset'] and name in ['readRaster','writeRaster','readBlock','writeBlock']:
            payload=', '.join('"'+n+'" to '+kt_arg(n,t) for n,t in params if n!='buffer')
            body=f'return rasterCall("{cls}", "{sig}", buffer, mapOf({payload}), {str(name.startswith("write")).lower()})'
        elif cls=='CoordinateTransformation' and name=='transformPoints':
            body='coordinateCall(points)'
        elif cls=='FeatureColumnStream' and name=='read':
            body='return columnRead()'
        elif cls=='MDArray' and name=='read':
            body='return mdReadRasterCall(start, count)'
        elif cls=='MDArray' and name=='write':
            body='return mdWriteRasterCall(start, count, values)'
        elif cls in ['MDArray','Attribute'] and name=='readValues':
            body=f'return mdReadValuesCall("{cls}", "{sig}", mapOf({payload}))'
        elif cls in ['MDArray','Attribute'] and name=='writeValues':
            payload=', '.join('"'+n+'" to '+kt_arg(n,t) for n,t in params if n!='values')
            body=f'return mdWriteValuesCall("{cls}", "{sig}", mapOf({payload}), values)'
        elif cls=='GdalRuntime' and name=='writeMemoryFile':
            body='writeMemoryFileCall(path, bytes)'
        elif cls=='GdalRuntime' and name=='readMemoryFile':
            body='return readMemoryFileCall(path)'
        elif cls=='GdalRuntime' and name=='openDataset' and params[0][1]=='GdalFiles':
            body=f'val result = fileInputCall("{cls}", "{sig}", mapOf("options" to encode(options)), files); return requireNotNull(engine.wrapDataset(result, null))'
        elif ret=='GdalFiles':
            body=f'return fileOutputCall("{cls}", "{sig}", mapOf({payload}))'
        elif cls=='Layer' and name=='openColumnStream':
            body=f'require(batchSize in 1..65536) {{ "Column batch size must be between 1 and 65536" }}; check(engine.supportsBinaryColumns) {{ "Column transfer requires engine binary protocol 3; use Feature iteration with older engines" }}; val result = {call}; return requireNotNull(engine.wrapFeatureColumnStream(result, this))'
        elif cls=='CoordinateTransformation' and name=='transformBounds':
            body=f'require(bounds.size >= 4); {call}.jsonArray.forEachIndexed {{ i, v -> bounds[i] = v.jsonPrimitive.content.toDouble() }}'
        elif ret=='Unit':
            body=call+'; return Unit'
        else:
            parent='this' if (cls,name) in PARENT_OWNED else 'null' if (cls,name) in OWNED else ('null' if cls=='GdalRuntime' else 'this')
            body='val result = '+call+'; return '+kt_result(ret,'result',parent)
        kt.append(f'    override suspend fun {name}({argstr}): {ret} {{ {body} }}')
        cpp.append(f'    if(type=="{cls}" && method=="{sig}") {{')
        custom=SPECIAL.get((cls,sig), SPECIAL.get((cls,name)))
        if custom:
            cast={'Group':'GDALGroupH','MDArray':'GDALMDArrayH','Dimension':'GDALDimensionH','Attribute':'GDALAttributeH','FieldDomain':'OGRFieldDomainH'}.get(cls)
            if cast:custom=re.sub(r'\bh\b',f'static_cast<{cast}>(h)',custom)
            cpp.append('        '+custom)
        else:
            fn=ALIASES.get(cls,{}).get(sig,ALIASES.get(cls,{}).get(name,PREFIX[cls]+name[0].upper()+name[1:]))
            callargs=[] if cls=='GdalRuntime' else ['Arg(r,h)']
            # Named field overloads resolve to the same indexed GDAL C API.
            for n,t in params:
                if cls=='Feature' and t=='String' and n=='name' and name not in ['getFieldIndex','getGeomFieldIndex']:
                    indexfn='OGR_F_GetGeomFieldIndex' if 'GeomField' in name else 'OGR_F_GetFieldIndex'
                    callargs.append(f'Arg(r, {indexfn}(h,a["name"].ToString().c_str()))')
                else:callargs.append(f'Arg(r,a["{n}"])')
            if cls=='Feature' and name=='setField':fn='OGR_F_SetField'+{'String':'String','Int':'Integer','Double':'Double'}[params[-1][1]]
            if cls=='SpatialReference' and name in ['setAxisMappingStrategy']:pass
            if cls=='Feature' and name=='getFieldType':
                idx='a.GetInteger("index")' if params[0][1]=='Int' else 'OGR_F_GetFieldIndex(h,a["name"].ToString().c_str())'
                cpp.append(f'        return value(OGR_Fld_GetType(OGR_F_GetFieldDefnRef(h,{idx})));');cpp.append('    }');continue
            if cls=='Feature' and name in ['setGeometryDirectly','setGeomFieldDirectly']:
                # Copy into the feature, then release the transferred source only on success.
                fn='OGR_F_SetGeometry' if name=='setGeometryDirectly' else 'OGR_F_SetGeomField'
            if cls=='Geometry' and name=='addGeometryDirectly':fn='OGR_G_AddGeometry'
            if cls=='Dataset' and name=='flushCache':fn='GDALFlushCache'
            if cls=='Geometry' and name=='addPoint' and len(params)==2:fn='OGR_G_AddPoint_2D'
            if cls=='Geometry' and name=='setPoint' and len(params)==3:fn='OGR_G_SetPoint_2D'
            if cls=='Geometry' and name=='addPoint2D':fn='OGR_G_AddPoint_2D'
            if cls=='Geometry' and name=='setPoint2D':fn='OGR_G_SetPoint_2D'
            expr=f'apply({fn}, '+', '.join(callargs)+')'
            t=ret.rstrip('?')
            if t in TYPES:
                owned=(cls,name) in OWNED
                cpp.append(f'        return r.result({expr},"{t}",{str(owned).lower()},'+('0' if owned or cls=='GdalRuntime' else 'id')+');')
            elif ret=='Unit':cpp.append(f'        {expr}; return nullValue();')
            elif ret=='Boolean':cpp.append(f'        return value({expr}!=0);')
            elif ret=='Long':cpp.append(f'        return value(std::to_string({expr}));')
            elif t=='List<String>':raise ValueError(('needs special',cls,name))
            else:
                cpp.append(f'        auto out={expr};'+('if(out==0) r.close(std::stoull(a["geometry"].GetString("handle")));' if name in ['setGeometryDirectly','setGeomFieldDirectly','addGeometryDirectly'] else '')+'return value(out);')
        cpp.append('    }')
    kt.append('}\n')
kt.append('internal fun RuntimeEngine.wrap(type: String, value: JsonElement, parent: RuntimeHandle?): RuntimeHandle? {\n    if (value is JsonNull) return null\n    val id = value.jsonPrimitive.content\n    return when(type) {')
for cls in TYPES[1:]:kt.append(f'        "{cls}" -> Bundled{cls}(this, id, parent)')
kt.append('        else -> error("Unknown GDAL handle type: $type")\n    }\n}')
for cls in TYPES[1:]:kt.append(f'internal fun RuntimeEngine.wrap{cls}(value: JsonElement, parent: RuntimeHandle?): {cls}? = wrap("{cls}", value, parent) as {cls}?')
(ROOT/'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/BundledRuntime.generated.kt').write_text('\n'.join(kt)+'\n')
(ROOT/'gdal4k/native/runtime.generated.inc').write_text('// Generated by gdal4k-engine/tools/generate-runtime.py\n'+'\n'.join(cpp)+'\n')

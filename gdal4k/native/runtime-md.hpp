// Portable structured values. GDAL ABI layout and allocated string pointers never leave the engine.
#pragma once

struct MDType {
    GDALExtendedDataTypeH handle=nullptr;
    std::vector<std::unique_ptr<MDType>> children;
    std::vector<std::string> names;
    std::vector<size_t> offsets;
    explicit MDType(GDALExtendedDataTypeH h):handle(h) {
        require(h!=nullptr,"Cannot construct multidimensional data type");
        if(kind()==GEDTC_COMPOUND) {
            size_t n=0;auto components=GDALExtendedDataTypeGetComponents(h,&n);
            // Each component and each returned type is an owned copy.
            struct Cleanup { GDALEDTComponentH* p;size_t n;~Cleanup(){GDALExtendedDataTypeFreeComponents(p,n);} } cleanup{components,n};
            for(size_t i=0;i<n;++i) {
                names.emplace_back(GDALEDTComponentGetName(components[i]));offsets.push_back(GDALEDTComponentGetOffset(components[i]));
                children.emplace_back(std::make_unique<MDType>(GDALEDTComponentGetType(components[i])));
            }
        }
    }
    ~MDType(){GDALExtendedDataTypeRelease(handle);}
    MDType(const MDType&)=delete;
    GDALExtendedDataTypeClass kind()const{return GDALExtendedDataTypeGetClass(handle);}
    size_t size()const{return GDALExtendedDataTypeGetSize(handle);}
    size_t alignment()const {
        if(kind()==GEDTC_STRING)return alignof(char*);
        if(kind()==GEDTC_NUMERIC){auto t=GDALExtendedDataTypeGetNumericDataType(handle);return std::min<size_t>(8,GDALGetDataTypeSizeBytes(GDALGetNonComplexDataType(t)));}
        size_t a=1;for(auto& c:children)a=std::max(a,c->alignment());return a;
    }
    void freeValue(char* p)const {
        if(kind()==GEDTC_STRING){char* text=nullptr;std::memcpy(&text,p,sizeof(text));CPLFree(text);}
        else if(kind()==GEDTC_COMPOUND)for(size_t i=0;i<children.size();++i)children[i]->freeValue(p+offsets[i]);
    }
};
inline size_t mdAligned(size_t n,size_t alignment){require(n<=SIZE_MAX-alignment,"Compound schema exceeds addressable memory");return (n+alignment-1)/alignment*alignment;}
inline std::unique_ptr<MDType> mdType(const CPLJSONObject& schema,unsigned depth=0) {
    require(depth<32,"Multidimensional schema nesting exceeds 32");auto kind=schema.GetString("kind");
    if(kind=="numeric") {auto t=schema.GetInteger("type");require(t>GDT_Unknown&&t<GDT_TypeCount,"Invalid GDAL numeric type");return std::make_unique<MDType>(GDALExtendedDataTypeCreate(static_cast<GDALDataType>(t)));}
    if(kind=="text") {auto n=schema.GetLong("maxLength");require(n>=0&&static_cast<uint64_t>(n)<=SIZE_MAX,"Invalid string maximum length");return std::make_unique<MDType>(GDALExtendedDataTypeCreateString(static_cast<size_t>(n)));}
    require(kind=="compound","Unknown multidimensional schema");auto components=schema.GetArray("components");require(components.Size()>0,"Compound type must have components");
    std::vector<std::unique_ptr<MDType>> types;std::vector<GDALEDTComponentH> fields;size_t size=0,alignment=1;std::set<std::string> names;
    struct Cleanup {std::vector<GDALEDTComponentH>& fields;~Cleanup(){for(auto f:fields)GDALEDTComponentRelease(f);}} cleanup{fields};
    for(auto c:components) {
        auto name=c.GetString("name");require(!name.empty()&&names.insert(name).second,"Compound component names must be nonempty and unique");
        auto t=mdType(c.GetObj("type"),depth+1);alignment=std::max(alignment,t->alignment());size=mdAligned(size,t->alignment());
        fields.push_back(GDALEDTComponentCreate(name.c_str(),size,t->handle));require(size<=SIZE_MAX-t->size(),"Compound type exceeds addressable memory");size+=t->size();types.push_back(std::move(t));
    }
    auto name=schema.GetString("name");require(!name.empty(),"Compound type name is empty");
    return std::make_unique<MDType>(GDALExtendedDataTypeCreateCompound(name.c_str(),mdAligned(size,alignment),fields.size(),fields.data()));
}
inline CPLJSONObject mdSchema(const MDType& type) {
    CPLJSONObject out;
    if(type.kind()==GEDTC_NUMERIC){out.Add("kind","numeric");out.Add("type",GDALExtendedDataTypeGetNumericDataType(type.handle));}
    else if(type.kind()==GEDTC_STRING){out.Add("kind","text");out.Add("maxLength",std::to_string(GDALExtendedDataTypeGetMaxStringLength(type.handle)));}
    else {out.Add("kind","compound");out.Add("name",GDALExtendedDataTypeGetName(type.handle));CPLJSONArray fields;for(size_t i=0;i<type.children.size();++i){CPLJSONObject f;f.Add("name",type.names[i]);f.Add("type",mdSchema(*type.children[i]));fields.Add(f);}out.Add("components",fields);}
    return out;
}
inline CPLJSONObject extendedTypeResult(GDALExtendedDataTypeH h){MDType type(h);CPLJSONObject out;out.Add("value",mdSchema(type));return out;}
template<class T> inline T mdLoad(const char* p){T v;std::memcpy(&v,p,sizeof(v));return v;}
template<class T> inline void mdStore(char* p,T v){std::memcpy(p,&v,sizeof(v));}
template<class T> inline T mdParse(const std::string& s) {
    T value{};
    if constexpr(std::is_integral_v<T>) {
        auto result=std::from_chars(s.data(),s.data()+s.size(),value);require(result.ec==std::errc()&&result.ptr==s.data()+s.size(),"Integer value is invalid or outside its data type");
    } else {
        if(s=="NaN")return std::numeric_limits<T>::quiet_NaN();if(s=="Infinity")return std::numeric_limits<T>::infinity();if(s=="-Infinity")return -std::numeric_limits<T>::infinity();
        std::istringstream in(s);in.imbue(std::locale::classic());in>>std::noskipws>>value;require(!in.fail()&&in.eof(),"Floating-point value is invalid or outside its data type");
    }
    return value;
}
template<class T> inline std::string mdFormat(T value) {
    if constexpr(std::is_integral_v<T>)return std::to_string(value);
    else {if(!std::isfinite(value))return std::isnan(value)?"NaN":value>0?"Infinity":"-Infinity";std::ostringstream out;out.imbue(std::locale::classic());out<<std::setprecision(std::numeric_limits<T>::max_digits10)<<value;return out.str();}
}
inline void mdPutNumber(char* p,GDALDataType type,const std::string& s) {
    switch(type) {
    case GDT_Float16:{auto v=mdParse<float>(s);auto half=CPLConvertFloatToHalf(v);require(!std::isfinite(v)||std::isfinite(CPLConvertHalfToFloat(half)),"Float16 value is outside its data type");mdStore(p,half);break;}
#define MD_PUT(g,t) case g:mdStore<t>(p,mdParse<t>(s));break;
    MD_PUT(GDT_Byte,uint8_t) MD_PUT(GDT_Int8,int8_t) MD_PUT(GDT_UInt16,uint16_t) MD_PUT(GDT_Int16,int16_t)
    MD_PUT(GDT_UInt32,uint32_t) MD_PUT(GDT_Int32,int32_t) MD_PUT(GDT_UInt64,uint64_t) MD_PUT(GDT_Int64,int64_t)
    MD_PUT(GDT_Float32,float) MD_PUT(GDT_Float64,double)
#undef MD_PUT
    default:throw std::runtime_error("Unsupported numeric component type");
    }
}
inline std::string mdGetNumber(const char* p,GDALDataType type) {
    switch(type) {
    case GDT_Float16:return mdFormat(CPLConvertHalfToFloat(mdLoad<uint16_t>(p)));
#define MD_GET(g,t) case g:return mdFormat(mdLoad<t>(p));
    MD_GET(GDT_Byte,uint8_t) MD_GET(GDT_Int8,int8_t) MD_GET(GDT_UInt16,uint16_t) MD_GET(GDT_Int16,int16_t)
    MD_GET(GDT_UInt32,uint32_t) MD_GET(GDT_Int32,int32_t) MD_GET(GDT_UInt64,uint64_t) MD_GET(GDT_Int64,int64_t)
    MD_GET(GDT_Float32,float) MD_GET(GDT_Float64,double)
#undef MD_GET
    default:throw std::runtime_error("Unsupported numeric component type");
    }
}
inline void mdPut(char* p,const MDType& type,const CPLJSONObject& value) {
    if(type.kind()==GEDTC_STRING) {
        require(value.GetString("kind")=="text","Expected text value");auto v=value["text"];char* text=nullptr;
        if(v.GetType()!=CPLJSONObject::Type::Null){auto s=v.ToString();auto maximum=GDALExtendedDataTypeGetMaxStringLength(type.handle);require(maximum==0||s.size()<=maximum,"Text exceeds maximum UTF-8 byte length");text=CPLStrdup(s.c_str());}mdStore(p,text);
    } else if(type.kind()==GEDTC_NUMERIC) {
        require(value.GetString("kind")=="numeric","Expected numeric value");auto numeric=GDALExtendedDataTypeGetNumericDataType(type.handle);auto base=GDALGetNonComplexDataType(numeric);bool complex=GDALDataTypeIsComplex(numeric);
        auto imaginary=value["imaginary"];require(complex==(imaginary.GetType()!=CPLJSONObject::Type::Null),"Imaginary component must match numeric data type");
        mdPutNumber(p,base,value.GetString("real"));if(complex)mdPutNumber(p+GDALGetDataTypeSizeBytes(base),base,imaginary.ToString());
    } else {
        require(value.GetString("kind")=="record","Expected record value");auto fields=value.GetArray("fields");require(fields.Size()==type.children.size(),"Record fields do not match schema");
        for(size_t i=0;i<type.children.size();++i)mdPut(p+type.offsets[i],*type.children[i],fields[static_cast<int>(i)]);
    }
}
inline CPLJSONObject mdGet(const char* p,const MDType& type) {
    CPLJSONObject out;
    if(type.kind()==GEDTC_STRING){out.Add("kind","text");auto s=mdLoad<char*>(p);if(s)out.Add("text",s);else out.AddNull("text");}
    else if(type.kind()==GEDTC_NUMERIC){out.Add("kind","numeric");auto numeric=GDALExtendedDataTypeGetNumericDataType(type.handle);auto base=GDALGetNonComplexDataType(numeric);out.Add("real",mdGetNumber(p,base));if(GDALDataTypeIsComplex(numeric))out.Add("imaginary",mdGetNumber(p+GDALGetDataTypeSizeBytes(base),base));else out.AddNull("imaginary");}
    else {out.Add("kind","record");CPLJSONArray fields;for(size_t i=0;i<type.children.size();++i)fields.Add(mdGet(p+type.offsets[i],*type.children[i]));out.Add("fields",fields);}
    return out;
}
struct MDBuffer {
    const MDType& type;size_t count;std::vector<char> data;
    MDBuffer(const MDType& type,size_t count):type(type),count(count) {require(type.size()>0&&count<=SIZE_MAX/type.size(),"Values exceed addressable memory");data.resize(count*type.size(),0);}
    ~MDBuffer(){for(size_t i=0;i<count;++i)type.freeValue(data.data()+i*type.size());}
    void put(const CPLJSONArray& values){require(values.Size()==count,"Values size does not match shape");for(size_t i=0;i<count;++i)mdPut(data.data()+i*type.size(),type,values[static_cast<int>(i)]);}
    CPLJSONObject get(){CPLJSONArray values;for(size_t i=0;i<count;++i)values.Add(mdGet(data.data()+i*type.size(),type));return value(values);}
};
// Protocol 5 carries public numeric component strings as length-prefixed ASCII.
// This preserves mdParse/mdFormat (including exact integers, complex values and
// nonfinite spellings) while removing repeated JSON objects/keys/escaping.
inline GIntBig mdValueCount(const CPLJSONObject& a) {
    auto kind=a["numericValueCount"].GetType();
    require(kind==CPLJSONObject::Type::Integer||kind==CPLJSONObject::Type::Long,"Numeric MD count must be an integer");
    auto count=a.GetLong("numericValueCount",-1);require(count>=0,"Invalid numeric MD count");return count;
}
inline std::string mdBinaryComponent(size_t& offset,bool nullable,bool& missing) {
    require(activeBinary->length-offset>=4,"Truncated numeric MD component length");uint32_t length=0;
    for(unsigned i=0;i<4;++i)length|=uint32_t(activeBinary->input[offset++])<<(8*i);
    missing=nullable&&length==UINT32_MAX;if(missing)return {};
    require(length<=INT_MAX&&length<=activeBinary->length-offset,"Invalid numeric MD component size");
    std::string text(length,'\0');
    for(size_t position=0;position<length;) {
        if(position)binaryCheckpoint();auto size=std::min<size_t>(262144,length-position);
        for(size_t i=0;i<size;++i)require(activeBinary->input[offset+position+i]<128,"Numeric MD component must be ASCII");
        std::memcpy(text.data()+position,activeBinary->input+offset+position,size);position+=size;
    }
    offset+=length;return text;
}
inline void mdBinaryPut(MDBuffer& buffer,const CPLJSONObject& a) {
    require(buffer.type.kind()==GEDTC_NUMERIC,"Numeric MD input requires a numeric schema");
    require(static_cast<uint64_t>(mdValueCount(a))==buffer.count,"Numeric MD values size does not match shape");
    require(buffer.count<=activeBinary->length/8,"Truncated numeric MD values");size_t offset=0;
    auto numeric=GDALExtendedDataTypeGetNumericDataType(buffer.type.handle);auto base=GDALGetNonComplexDataType(numeric);bool complex=GDALDataTypeIsComplex(numeric);
    for(size_t i=0;i<buffer.count;++i) {
        if((i&1023)==0)binaryCheckpoint();bool missing=false;
        auto real=mdBinaryComponent(offset,false,missing);auto imaginary=mdBinaryComponent(offset,true,missing);
        require(complex==!missing,"Imaginary component must match numeric data type");
        char* p=buffer.data.data()+i*buffer.type.size();mdPutNumber(p,base,real);
        if(complex)mdPutNumber(p+GDALGetDataTypeSizeBytes(base),base,imaginary);
    }
    require(offset==activeBinary->length,"Trailing numeric MD input bytes");
}
inline void mdBinaryAppend(const std::string& text,bool missing=false) {
    auto& output=activeBinary->output;
    require(text.size()<=INT_MAX&&output.size()<=static_cast<size_t>(INT_MAX)-4&&text.size()<=static_cast<size_t>(INT_MAX)-4-output.size(),"Numeric MD response exceeds bridge size; read smaller windows");
    uint32_t length=missing?UINT32_MAX:static_cast<uint32_t>(text.size());
    for(unsigned i=0;i<4;++i)output.push_back(static_cast<unsigned char>(length>>(8*i)));
    auto offset=output.size();output.resize(offset+text.size());
    for(size_t position=0;position<text.size();) {
        if(position)binaryCheckpoint();auto size=std::min<size_t>(262144,text.size()-position);
        std::memcpy(output.data()+offset+position,text.data()+position,size);position+=size;
    }
}
inline CPLJSONObject mdBinaryGet(const MDType& type,const char* data,size_t count) {
    require(count<=INT_MAX,"Numeric MD count exceeds bridge limit");auto numeric=GDALExtendedDataTypeGetNumericDataType(type.handle);auto base=GDALGetNonComplexDataType(numeric);bool complex=GDALDataTypeIsComplex(numeric);
    for(size_t i=0;i<count;++i) {
        if((i&1023)==0)binaryCheckpoint();auto p=data+i*type.size();
        mdBinaryAppend(mdGetNumber(p,base));
        if(complex)mdBinaryAppend(mdGetNumber(p+GDALGetDataTypeSizeBytes(base),base));else mdBinaryAppend({},true);
    }
    CPLJSONObject metadata,result;metadata.Add("binaryMDValues",true);metadata.Add("count",static_cast<int>(count));result.Add("value",metadata);return result;
}
inline CPLJSONObject createExtendedArray(Runtime& r,GDALGroupH group,const CPLJSONObject& a,unsigned long long id) {
    auto type=mdType(a.GetObj("dataType"));std::vector<GDALDimensionH> dims;for(auto d:a.GetArray("dimensions"))dims.push_back(static_cast<GDALDimensionH>(r.arg(d)));
    Strings opts(a.GetArray("options"));return r.result(GDALGroupCreateMDArray(group,a.GetString("name").c_str(),dims.size(),dims.data(),type->handle,opts.data()),"MDArray",true,id);
}
inline CPLJSONObject mdValues(GDALMDArrayH h,const CPLJSONObject& a,bool write) {
    MDType type(GDALMDArrayGetDataType(h));auto starts=a.GetArray("start"),counts=a.GetArray("count");size_t nd=0;auto dims=GDALMDArrayGetDimensions(h,&nd);
    struct Cleanup {GDALDimensionH* p;size_t n;~Cleanup(){GDALReleaseDimensions(p,n);}} cleanup{dims,nd};
    require(starts.Size()==nd&&counts.Size()==nd,"Slice rank does not match array");std::vector<GUInt64> start;std::vector<size_t> count;size_t n=1;
    for(size_t i=0;i<nd;++i){auto s=starts[static_cast<int>(i)].ToLong(),c=counts[static_cast<int>(i)].ToLong();auto size=GDALDimensionGetSize(dims[i]);require(s>=0&&c>0&&static_cast<uint64_t>(s)<=size&&static_cast<uint64_t>(c)<=size-s,"Slice is outside array");require(c<=INT_MAX&&n<=static_cast<size_t>(INT_MAX)/c,"Slice exceeds bridge limit; read smaller windows");start.push_back(s);count.push_back(c);n*=c;}
    if(activeBinary&&!write)require(activeBinary->length==0,"MD reads cannot have an input payload");
    MDBuffer buffer(type,n);if(write&&activeBinary)mdBinaryPut(buffer,a);else if(write)buffer.put(a.GetArray("values"));
    auto ok=write?GDALMDArrayWrite(h,start.data(),count.data(),nullptr,nullptr,type.handle,buffer.data.data(),buffer.data.data(),buffer.data.size()):GDALMDArrayRead(h,start.data(),count.data(),nullptr,nullptr,type.handle,buffer.data.data(),buffer.data.data(),buffer.data.size());
    if(write)return value(ok!=0);require(ok!=0,"Multidimensional read failed");if(activeBinary&&type.kind()==GEDTC_NUMERIC)return mdBinaryGet(type,buffer.data.data(),n);return buffer.get();
}
inline CPLJSONObject attributeOperation(Runtime& r,void* h,const CPLJSONObject& a,unsigned long long id,const std::string& owner,const std::string& op) {
    bool group=owner=="Group";auto g=static_cast<GDALGroupH>(h);auto m=static_cast<GDALMDArrayH>(h);auto name=a.GetString("name");Strings opts(a.GetArray("options"));
    if(op=="getAttribute")return r.result(group?GDALGroupGetAttribute(g,name.c_str()):GDALMDArrayGetAttribute(m,name.c_str()),"Attribute",true,id);
    if(op=="getAttributes") {
        size_t n=0;auto list=group?GDALGroupGetAttributes(g,&n,opts.data()):GDALMDArrayGetAttributes(m,&n,opts.data());CPLJSONArray out;
        for(size_t i=0;i<n;++i)out.Add(r.result(list[i],"Attribute",true,id)["value"]);CPLFree(list);return value(out);
    }
    if(op=="deleteAttribute") {
        auto root=[&](unsigned long long key){while(r.handles.at(key).parent)key=r.handles.at(key).parent;return key;};
        std::vector<unsigned long long> ids;
        // Resolve aliases before deletion: some drivers invalidate the native object immediately.
        for(auto& e:r.handles)if(e.second.type=="Attribute"&&root(e.first)==root(id)) {
            auto parent=r.handles.find(e.second.parent);
            if(parent!=r.handles.end()&&parent->second.type==owner) {
                auto parentName=group?GDALGroupGetFullName(static_cast<GDALGroupH>(parent->second.pointer)):GDALMDArrayGetFullName(static_cast<GDALMDArrayH>(parent->second.pointer));
                auto thisName=group?GDALGroupGetFullName(g):GDALMDArrayGetFullName(m);
                if(std::string(parentName)==thisName&&std::string(GDALAttributeGetName(static_cast<GDALAttributeH>(e.second.pointer)))==name)ids.push_back(e.first);
            }
        }
        auto ok=group?GDALGroupDeleteAttribute(g,name.c_str(),opts.data()):GDALMDArrayDeleteAttribute(m,name.c_str(),opts.data());
        if(ok)for(auto i:ids)r.close(i);return value(ok!=0);
    }
    auto type=mdType(a.GetObj("dataType"));std::vector<GUInt64> dims;size_t n=1;for(auto d:a.GetArray("dimensions")){auto size=d.ToLong();require(size>0&&size<=INT_MAX&&n<=static_cast<size_t>(INT_MAX)/size,"Attribute shape exceeds bridge limit");dims.push_back(size);n*=size;}
    return r.result(group?GDALGroupCreateAttribute(g,name.c_str(),dims.size(),dims.data(),type->handle,opts.data()):GDALMDArrayCreateAttribute(m,name.c_str(),dims.size(),dims.data(),type->handle,opts.data()),"Attribute",true,id);
}
inline CPLJSONObject attributeValues(GDALAttributeH h,const CPLJSONObject& a,bool write) {
    MDType type(GDALAttributeGetDataType(h));auto count=GDALAttributeGetTotalElementsCount(h);require(count<=INT_MAX,"Attribute exceeds bridge limit");
    if(write){MDBuffer buffer(type,static_cast<size_t>(count));if(activeBinary)mdBinaryPut(buffer,a);else buffer.put(a.GetArray("values"));return value(GDALAttributeWriteRaw(h,buffer.data.data(),buffer.data.size())!=0);}
    if(activeBinary)require(activeBinary->length==0,"Attribute reads cannot have an input payload");
    size_t size=0;auto p=GDALAttributeReadAsRaw(h,&size);require(p!=nullptr,"Cannot read attribute");
    struct Cleanup {GDALAttributeH h;GByte* p;size_t size;~Cleanup(){GDALAttributeFreeRawResult(h,p,size);}} cleanup{h,p,size};
    require(type.size()>0&&count<=SIZE_MAX/type.size()&&size==count*type.size(),"Unexpected attribute buffer size");if(activeBinary&&type.kind()==GEDTC_NUMERIC)return mdBinaryGet(type,reinterpret_cast<char*>(p),static_cast<size_t>(count));CPLJSONArray values;for(size_t i=0;i<count;++i)values.Add(mdGet(reinterpret_cast<char*>(p)+i*type.size(),type));return value(values);
}
inline CPLJSONObject createCodedDomain(Runtime& r,const CPLJSONObject& a) {
    std::vector<std::string> codes,labels;std::vector<bool> hasLabel;std::set<std::string> unique;
    for(auto v:a.GetArray("values")){auto code=v.GetString("code");require(unique.insert(code).second,"Duplicate enumeration code");codes.push_back(code);labels.push_back(v.GetString("label"));hasLabel.push_back(v["label"].GetType()!=CPLJSONObject::Type::Null);}
    std::vector<OGRCodedValue> values;for(size_t i=0;i<codes.size();++i)values.push_back({codes[i].data(),hasLabel[i]?labels[i].data():nullptr});values.push_back({nullptr,nullptr});
    return r.result(OGR_CodedFldDomain_Create(a.GetString("name").c_str(),a.GetString("description").c_str(),static_cast<OGRFieldType>(a.GetInteger("fieldType")),static_cast<OGRFieldSubType>(a.GetInteger("fieldSubType")),values.data()),"FieldDomain",true);
}
inline CPLJSONObject codedDomainValues(OGRFieldDomainH h) {
    if(OGR_FldDomain_GetDomainType(h)!=OFDT_CODED)return nullValue();CPLJSONArray values;auto list=OGR_CodedFldDomain_GetEnumeration(h);
    for(size_t i=0;list&&list[i].pszCode;++i){CPLJSONObject v;v.Add("code",list[i].pszCode);if(list[i].pszValue)v.Add("label",list[i].pszValue);else v.AddNull("label");values.Add(v);}return value(values);
}
inline OGRField rangeValue(const CPLJSONObject& bound,OGRFieldType type) {
    OGRField out{};if(bound.GetType()==CPLJSONObject::Type::Null){OGR_RawField_SetUnset(&out);return out;}auto text=bound.GetString("value");
    if(type==OFTInteger)out.Integer=mdParse<int>(text);else if(type==OFTInteger64)out.Integer64=mdParse<int64_t>(text);else if(type==OFTReal){out.Real=mdParse<double>(text);require(!std::isnan(out.Real),"NaN is not a range bound");}
    else require(OGRParseDate(text.c_str(),&out,0)!=0,"Invalid date/time bound");return out;
}
inline CPLJSONObject createRangeDomain(Runtime& r,const CPLJSONObject& a) {
    auto type=static_cast<OGRFieldType>(a.GetInteger("fieldType"));require(type==OFTInteger||type==OFTInteger64||type==OFTReal||type==OFTDateTime,"Range domains require Integer, Integer64, Real or DateTime fields");
    auto minimum=rangeValue(a["minimum"],type),maximum=rangeValue(a["maximum"],type);
    return r.result(OGR_RangeFldDomain_Create(a.GetString("name").c_str(),a.GetString("description").c_str(),type,static_cast<OGRFieldSubType>(a.GetInteger("fieldSubType")),&minimum,a["minimum"].GetBool("inclusive"),&maximum,a["maximum"].GetBool("inclusive")),"FieldDomain",true);
}
inline CPLJSONObject rangeDomainBound(OGRFieldDomainH h,bool minimum) {
    if(OGR_FldDomain_GetDomainType(h)!=OFDT_RANGE)return nullValue();bool inclusive=false;auto bound=minimum?OGR_RangeFldDomain_GetMin(h,&inclusive):OGR_RangeFldDomain_GetMax(h,&inclusive);if(!bound||OGR_RawField_IsUnset(bound))return nullValue();auto type=OGR_FldDomain_GetFieldType(h);std::string text;
    if(type==OFTInteger)text=std::to_string(bound->Integer);else if(type==OFTInteger64)text=std::to_string(bound->Integer64);else if(type==OFTReal)text=mdFormat(bound->Real);
    else {auto def=OGR_FD_Create("bound");auto field=OGR_Fld_Create("value",OFTDateTime);OGR_FD_AddFieldDefn(def,field);OGR_Fld_Destroy(field);auto feature=OGR_F_Create(def);OGR_F_SetFieldRaw(feature,0,bound);text=OGR_F_GetFieldAsISO8601DateTime(feature,0,nullptr);OGR_F_Destroy(feature);}
    CPLJSONObject result,b; b.Add("value",text);b.Add("inclusive",inclusive);result.Add("value",b);return result;
}

// GDAL 3.12 GeoPackage persists range endpoints as REAL, even for Integer64 domains.
inline void validateDomainStorage(GDALDatasetH dataset,OGRFieldDomainH domain) {
    if(std::string(GDALGetDriverShortName(GDALGetDatasetDriver(dataset)))!="GPKG"||OGR_FldDomain_GetDomainType(domain)!=OFDT_RANGE||OGR_FldDomain_GetFieldType(domain)!=OFTInteger64)return;
    for(bool minimum:{true,false}) {
        bool inclusive=false;auto bound=minimum?OGR_RangeFldDomain_GetMin(domain,&inclusive):OGR_RangeFldDomain_GetMax(domain,&inclusive);
        if(!bound||OGR_RawField_IsUnset(bound))continue;
        uint64_t magnitude=bound->Integer64<0?uint64_t(0)-static_cast<uint64_t>(bound->Integer64):static_cast<uint64_t>(bound->Integer64);
        while(magnitude>(uint64_t(1)<<53)){require((magnitude&1)==0,"GeoPackage cannot persist this Int64 range bound without losing precision");magnitude>>=1;}
    }
}

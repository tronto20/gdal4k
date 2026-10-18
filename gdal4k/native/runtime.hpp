#pragma once
#include <gdal_priv.h>
#include <gdal_alg.h>
#include <ogrsf_frmts.h>
#include <gdal_utils.h>
#include <cpl_vsi.h>
#include <ogr_api.h>
#include <ogr_srs_api.h>
#include <ogrsf_frmts.h>
#include <cpl_json.h>
#include <cpl_string.h>
#include <functional>
#include <map>
#include <memory>
#include <limits>
#include <stdexcept>
#include <type_traits>
#include <vector>
#include <atomic>
#include "binary.hpp"

#include <cstring>
#include <cctype>
#include <charconv>
#include <cpl_float.h>
#include <iomanip>
#include <sstream>
#include <set>

namespace g4k {
inline void require(bool ok,const char* message) { if(!ok) throw std::runtime_error(message); }
inline CPLJSONObject nullValue() { CPLJSONObject o; o.AddNull("value");return o; }
inline CPLJSONObject value(const char* v) { if(!v)return nullValue();CPLJSONObject o;o.Add("value",v);return o; }
inline CPLJSONObject value(const std::string& v) { return value(v.c_str()); }
inline CPLJSONObject value(const CPLJSONArray& v) { CPLJSONObject o;o.Add("value",v);return o; }
template<class T> CPLJSONObject value(T v) {
    CPLJSONObject o;
    if constexpr(std::is_enum_v<T>)o.Add("value",static_cast<int>(v));
    else if constexpr(std::is_floating_point_v<T>) {if(std::isfinite(v))o.Add("value",static_cast<double>(v));else o.Add("value",std::isnan(v)?"NaN":v>0?"Infinity":"-Infinity");}
    else if constexpr(std::is_integral_v<T> && sizeof(T)>4)o.Add("value",std::to_string(v));
    else o.Add("value",v);
    return o;
}
inline CPLJSONObject allocated(char* p) {auto out=value(p);CPLFree(p);return out;}
template<class T> CPLJSONObject numbers(const T* p,size_t n) {CPLJSONArray a;for(size_t i=0;i<n;i++)a.Add(value(p[i])["value"]);return value(a);}
inline CPLJSONObject numbers(std::initializer_list<double> v) {return numbers(v.begin(),v.size());}
inline CPLJSONObject strings(char** p) {CPLJSONArray a;for(int i=0;p&&p[i];i++)a.Add(p[i]);return value(a);}
// JSON numbers always use a dot, regardless of the application's LC_NUMERIC.
inline std::vector<double> doubles(const CPLJSONArray& a){std::vector<double> out;for(auto v:a)out.push_back(CPLAtof(v.ToString().c_str()));return out;}
inline std::vector<int> ints(const CPLJSONArray& a){std::vector<int> out;for(auto v:a)out.push_back(v.ToInteger());return out;}
struct Strings {
    char** list=nullptr;
    explicit Strings(const CPLJSONArray& a){for(auto v:a){auto s=v.ToString();require(s.find('\0')==std::string::npos,"NUL in GDAL string");list=CSLAddString(list,s.c_str());}}
    ~Strings(){CSLDestroy(list);}
    char** data(){return list;}
};
struct Runtime {
    struct Entry {void* pointer;std::string type;bool owned;unsigned long long parent;};
    std::map<unsigned long long,Entry> handles;
    std::map<unsigned long long,std::string> workspaces;
    // Dataset handle lifetime and native resource lifetime differ for lazy VRT sources.
    std::map<unsigned long long,size_t> datasetLeases;
    std::map<unsigned long long,Entry> deferredDatasets;
    std::map<unsigned long long,std::vector<unsigned long long>> datasetSources;
    std::vector<unsigned long long> releasedDatasets;
    unsigned long long next=0;
    ~Runtime(){while(!handles.empty())close(handles.rbegin()->first);}
    void* get(unsigned long long id,const std::string& type="") {
        auto it=handles.find(id);require(it!=handles.end(),"GDAL handle is closed");
        require(type.empty()||it->second.type==type,"GDAL handle has the wrong type");
        if(it->second.parent)get(it->second.parent);
        return it->second.pointer;
    }
    void* arg(const CPLJSONObject& v){return get(std::stoull(v.GetString("handle")),v.GetString("type"));}
    CPLJSONObject result(void* p,const char* type,bool owned,unsigned long long parent=0) {
        if(!p)return nullValue();auto id=++next;handles[id]={p,type,owned,parent};return value(std::to_string(id));
    }
    void invalidatePointer(void* pointer) {
        std::vector<unsigned long long> ids;
        for(auto& item:handles)if(item.second.pointer==pointer)ids.push_back(item.first);
        for(auto id:ids)close(id);
    }
    void releaseResultSets(void* dataset) {
        std::vector<unsigned long long> ids;
        for(auto& item:handles) {
            auto p=handles.find(item.second.parent);
            if(item.second.type=="Layer" && item.second.owned && p!=handles.end() && p->second.pointer==dataset)ids.push_back(item.first);
        }
        for(auto id:ids)close(id);
    }
    void invalidateChildren(void* parent,const std::string& type) {
        std::vector<unsigned long long> children;
        for(auto& item:handles) {
            auto p=handles.find(item.second.parent);
            if(p!=handles.end() && p->second.pointer==parent && item.second.type==type)children.push_back(item.first);
        }
        for(auto id:children)close(id);
    }
    void invalidateOverviews(void* dataset) {
        std::vector<void*> bands;
        for(auto& item:handles) {
            auto p=handles.find(item.second.parent);
            if(p!=handles.end() && p->second.pointer==dataset && item.second.type=="Band")bands.push_back(item.second.pointer);
        }
        for(auto band:bands)invalidateChildren(band,"Band");
    }
    void retainSources(unsigned long long id,const std::vector<unsigned long long>& sources) {
        std::set<unsigned long long> unique(sources.begin(),sources.end());
        datasetSources[id]={unique.begin(),unique.end()};
        for(auto source:unique)++datasetLeases[source];
    }
    void destroyDataset(unsigned long long id,void* pointer) {
        GDALClose(pointer);
        auto workspace=workspaces.find(id);
        if(workspace!=workspaces.end()){VSIRmdirRecursive(workspace->second.c_str());workspaces.erase(workspace);}
        releasedDatasets.push_back(id);
        auto inputs=datasetSources.find(id);
        if(inputs==datasetSources.end())return;
        auto sources=std::move(inputs->second);datasetSources.erase(inputs);
        for(auto source:sources) {
            auto lease=datasetLeases.find(source);
            if(lease!=datasetLeases.end()&&--lease->second==0) {
                datasetLeases.erase(lease);
                auto deferred=deferredDatasets.find(source);
                if(deferred!=deferredDatasets.end()) {
                    auto p=deferred->second.pointer;deferredDatasets.erase(deferred);destroyDataset(source,p);
                }
            }
        }
    }
    void close(unsigned long long id) {
        auto found=handles.find(id);if(found==handles.end())return;
        std::vector<unsigned long long> children;for(auto& item:handles)if(item.second.parent==id)children.push_back(item.first);
        for(auto child:children)close(child);
        auto e=found->second;handles.erase(found);if(!e.owned)return;
        if(e.type=="Dataset") {
            if(datasetLeases.count(id)) {
                GDALFlushCache(e.pointer);deferredDatasets[id]=e;
            } else destroyDataset(id,e.pointer);
        }
        else if(e.type=="Layer") {
            auto parent=handles.find(e.parent);
            if(parent!=handles.end())GDALDatasetReleaseResultSet(parent->second.pointer,e.pointer);
        }
        else if(e.type=="ColorTable")GDALDestroyColorTable(e.pointer);
        else if(e.type=="RasterAttributeTable")GDALDestroyRasterAttributeTable(e.pointer);
        else if(e.type=="Group")GDALGroupRelease(static_cast<GDALGroupH>(e.pointer));
        else if(e.type=="MDArray")GDALMDArrayRelease(static_cast<GDALMDArrayH>(e.pointer));
        else if(e.type=="Attribute")GDALAttributeRelease(static_cast<GDALAttributeH>(e.pointer));
        else if(e.type=="Dimension")GDALDimensionRelease(static_cast<GDALDimensionH>(e.pointer));
        else if(e.type=="FieldDomain")OGR_FldDomain_Destroy(static_cast<OGRFieldDomainH>(e.pointer));
        else if(e.type=="Relationship")GDALDestroyRelationship(e.pointer);
        else if(e.type=="Feature")OGR_F_Destroy(e.pointer);
        else if(e.type=="Geometry")OGR_G_DestroyGeometry(e.pointer);
        else if(e.type=="SpatialReference")OSRRelease(e.pointer);
        else if(e.type=="CoordinateTransformation")OCTDestroyCoordinateTransformation(e.pointer);
        else if(e.type=="FeatureDefn")OGR_FD_Release(e.pointer);
        else if(e.type=="FieldDefn")OGR_Fld_Destroy(e.pointer);
        else if(e.type=="GeomFieldDefn")OGR_GFld_Destroy(static_cast<OGRGeomFieldDefnH>(e.pointer));
        else if(e.type=="GCP"){GDALDeinitGCPs(1,static_cast<GDAL_GCP*>(e.pointer));CPLFree(e.pointer);}
        else throw std::runtime_error("Unknown owned GDAL handle type");
    }
    CPLJSONObject execute(const CPLJSONObject& request);
};
#include "runtime-files.hpp"
struct Arg {
    Runtime& r;CPLJSONObject json;void* pointer=nullptr;bool native=false;
    mutable std::unique_ptr<Strings> list;
    Arg(Runtime& r,const CPLJSONObject& json):r(r),json(json){}
    Arg(Runtime& r,void* p):r(r),pointer(p),native(true){}
    Arg(Runtime& r,int n):r(r),json(value(n)["value"]){}
    template<class T> operator T() const {
        if constexpr(std::is_pointer_v<T> && !std::is_same_v<T,const char*> && !std::is_same_v<T,char**> && !std::is_same_v<T,const char* const*>)return static_cast<T>(native?pointer:(json.GetType()==CPLJSONObject::Type::Null?nullptr:r.arg(json)));
        else if constexpr(std::is_same_v<T,const char*>){text=json.ToString();require(text.find('\0')==std::string::npos,"NUL in GDAL string");return text.c_str();}
        else if constexpr(std::is_same_v<T,char**>||std::is_same_v<T,const char* const*>){list=std::make_unique<Strings>(json.ToArray());return list->data();}
        else if constexpr(std::is_enum_v<T>)return static_cast<T>(json.ToInteger());
        else if constexpr(std::is_floating_point_v<T>)return static_cast<T>(CPLAtof(json.ToString().c_str()));
        else if constexpr(std::is_integral_v<T>)return static_cast<T>(json.GetType()==CPLJSONObject::Type::Boolean?json.ToBool():json.ToLong());
        else {static_assert(!sizeof(T),"An explicit GDAL mapping is required for this argument type");}
    }
    mutable std::string text;
};
template<class R,class... P,class... A> R apply(R(*fn)(P...),const A&... a){static_assert(sizeof...(P)==sizeof...(A),"Incorrect GDAL arity");return fn(static_cast<P>(a)...);}

template<class T> CPLJSONObject rasterTyped(GDALRasterBandH h,const CPLJSONObject& a,bool write,GDALDataType type){
    auto json=a.GetArray("buffer");auto o=a.GetObj("options");bool options=o.IsValid();
    auto region=options?o:a;int x=region.GetInteger("xOffset"),y=region.GetInteger("yOffset");
    require(x>=0 && y>=0 && x<GDALGetRasterBandXSize(h) && y<GDALGetRasterBandYSize(h),"Raster offset is outside band");
    int width=region["xSize"].IsValid()?region.GetInteger("xSize"):GDALGetRasterBandXSize(h);
    int height=region["ySize"].IsValid()?region.GetInteger("ySize"):GDALGetRasterBandYSize(h);
    if(options && width==0)width=GDALGetRasterBandXSize(h)-x;
    if(options && height==0)height=GDALGetRasterBandYSize(h)-y;
    int bw=options?o.GetInteger("bufferWidth"):width,bh=options?o.GetInteger("bufferHeight"):height;
    if(options && bw==0)bw=width;if(options && bh==0)bh=height;
    require(bw>0 && bh>0,"Raster buffer dimensions must be positive");
    require(x>=0&&y>=0&&width>0&&height>0&&x<=GDALGetRasterBandXSize(h)-width&&y<=GDALGetRasterBandYSize(h)-height,"Raster window is outside the band");
    // size_t is 32-bit in Wasm. Validate before narrowing
    // so a huge virtual raster cannot wrap into a small allocation passed to GDAL.
    const auto capacity = (activeBinary || !write) && a["bufferSize"].IsValid() ? a.GetLong("bufferSize") : json.Size();
    uint64_t count=static_cast<uint64_t>(bw)*bh;
    require(capacity>=0 && count<=static_cast<uint64_t>(capacity),"Raster buffer is too small");
    require(count<=INT_MAX,"Raster buffer exceeds bridge limit; read smaller windows");
    require(count<=SIZE_MAX/sizeof(T),"Raster buffer exceeds addressable memory");
    validateBinary(count,sizeof(T),write);
    std::vector<T> data(count);
    if(write && activeBinary)binaryInput(data.data(),count);
    else if(write)for(size_t i=0;i<count;i++){
        if constexpr(std::is_floating_point_v<T>)data[i]=static_cast<T>(CPLAtof(json[static_cast<int>(i)].ToString().c_str()));
        else data[i]=static_cast<T>(std::stoll(json[static_cast<int>(i)].ToString()));
    }
    GDALRasterIOExtraArg extra;INIT_RASTERIO_EXTRA_ARG(extra);
    int alg=options?o.GetInteger("resampling"):0;require((alg>=0 && alg<=7)||alg==14,"Unknown raster resampling algorithm");
    require(!write || alg==0,"Resampling selection is only supported for reads");
    extra.eResampleAlg=static_cast<GDALRIOResampleAlg>(alg);
    extra.pfnProgress=progress;extra.pProgressData=activeProgress;
    auto code=GDALRasterIOEx(h,write?GF_Write:GF_Read,x,y,width,height,data.data(),bw,bh,type,0,0,&extra);
    CPLJSONObject out;out.Add("code",code);if(!write && code==CE_None){if(activeBinary)binaryOutput(data.data(),data.size());else out.Add("buffer",numbers(data.data(),data.size())["value"]);}CPLJSONObject result;result.Add("value",out);return result;
}
inline CPLJSONObject raster(GDALRasterBandH h,const CPLJSONObject& a,bool write,const std::string& method){
    if(method.find("_ByteArray")!=std::string::npos)return rasterTyped<unsigned char>(h,a,write,GDT_Byte);
    if(method.find("_ShortArray")!=std::string::npos)return rasterTyped<short>(h,a,write,GDT_Int16);
    if(method.find("_IntArray")!=std::string::npos)return rasterTyped<int>(h,a,write,GDT_Int32);
    if(method.find("_LongArray")!=std::string::npos)return rasterTyped<int64_t>(h,a,write,GDT_Int64);
    if(method.find("_FloatArray")!=std::string::npos)return rasterTyped<float>(h,a,write,GDT_Float32);
    return rasterTyped<double>(h,a,write,GDT_Float64);
}
template<class T> CPLJSONObject datasetRasterTyped(GDALDatasetH h,const CPLJSONObject& a,bool write,GDALDataType type) {
    auto o=a.GetObj("options");auto bands=ints(o.GetArray("bands"));
    if(bands.empty())for(int i=1;i<=GDALGetRasterCount(h);++i)bands.push_back(i);
    require(!bands.empty(),"Dataset has no raster bands");
    for(auto b:bands)require(b>=1 && b<=GDALGetRasterCount(h),"Band index is outside dataset");
    int x=o.GetInteger("xOffset"),y=o.GetInteger("yOffset");
    int sx=GDALGetRasterXSize(h),sy=GDALGetRasterYSize(h);
    require(x>=0 && y>=0 && x<sx && y<sy,"Raster offset is outside dataset");
    int w=o.GetInteger("xSize"),v=o.GetInteger("ySize");if(w==0)w=sx-x;if(v==0)v=sy-y;
    require(w>0 && v>0 && w<=sx-x && v<=sy-y,"Raster window is outside dataset");
    int bw=o.GetInteger("bufferWidth"),bh=o.GetInteger("bufferHeight");if(bw==0)bw=w;if(bh==0)bh=v;
    require(bw>0 && bh>0,"Raster buffer dimensions must be positive");
    auto pixels=static_cast<uint64_t>(bw)*bh;
    require(pixels<=static_cast<uint64_t>(INT_MAX)/bands.size(),"Raster buffer exceeds bridge limit; read smaller windows");
    auto count=pixels*bands.size();auto json=a.GetArray("buffer");
    auto capacity=write&&!activeBinary?json.Size():a.GetLong("bufferSize");
    require(capacity>=0 && count<=static_cast<uint64_t>(capacity),"Raster buffer is too small");
    require(count<=SIZE_MAX/sizeof(T),"Raster buffer exceeds addressable memory");
    validateBinary(count,sizeof(T),write);
    std::vector<T> data(static_cast<size_t>(count));
    if(write && activeBinary)binaryInput(data.data(),count);
    else if(write)for(size_t i=0;i<count;++i) {
        if constexpr(std::is_floating_point_v<T>)data[i]=static_cast<T>(CPLAtof(json[static_cast<int>(i)].ToString().c_str()));
        else data[i]=static_cast<T>(std::stoll(json[static_cast<int>(i)].ToString()));
    }
    GDALRasterIOExtraArg extra;INIT_RASTERIO_EXTRA_ARG(extra);
    int alg=o.GetInteger("resampling");require((alg>=0 && alg<=7)||alg==14,"Unknown raster resampling algorithm");
    require(!write || alg==0,"Resampling selection is only supported for reads");
    extra.eResampleAlg=static_cast<GDALRIOResampleAlg>(alg);
    extra.pfnProgress=progress;extra.pProgressData=activeProgress;
    auto code=GDALDatasetRasterIOEx(h,write?GF_Write:GF_Read,x,y,w,v,data.data(),bw,bh,type,
        static_cast<int>(bands.size()),bands.data(),sizeof(T),static_cast<GSpacing>(bw)*sizeof(T),
        static_cast<GSpacing>(pixels)*sizeof(T),&extra);
    CPLJSONObject out;out.Add("code",code);
    if(!write && code==CE_None){if(activeBinary)binaryOutput(data.data(),data.size());else out.Add("buffer",numbers(data.data(),data.size())["value"]);}
    CPLJSONObject result;result.Add("value",out);return result;
}
inline CPLJSONObject datasetRaster(GDALDatasetH h,const CPLJSONObject& a,bool write,const std::string& method) {
    if(method.find("_ByteArray")!=std::string::npos)return datasetRasterTyped<unsigned char>(h,a,write,GDT_Byte);
    if(method.find("_ShortArray")!=std::string::npos)return datasetRasterTyped<short>(h,a,write,GDT_Int16);
    if(method.find("_IntArray")!=std::string::npos)return datasetRasterTyped<int>(h,a,write,GDT_Int32);
    if(method.find("_LongArray")!=std::string::npos)return datasetRasterTyped<int64_t>(h,a,write,GDT_Int64);
    if(method.find("_FloatArray")!=std::string::npos)return datasetRasterTyped<float>(h,a,write,GDT_Float32);
    return datasetRasterTyped<double>(h,a,write,GDT_Float64);
}
inline CPLJSONObject mdRaster(GDALMDArrayH h,const CPLJSONObject& a,bool write) {
    auto type=GDALMDArrayGetDataType(h);bool numeric=GDALExtendedDataTypeGetClass(type)==GEDTC_NUMERIC;GDALExtendedDataTypeRelease(type);
    require(numeric,"Only numeric multidimensional arrays support Float64 slices");
    size_t rank=GDALMDArrayGetDimensionCount(h);auto starts=a.GetArray("start"),counts=a.GetArray("count");
    require(starts.Size()==rank && counts.Size()==rank,"Slice rank does not match array");
    size_t nd=0;auto dims=GDALMDArrayGetDimensions(h,&nd);std::vector<GUInt64> shape;
    for(size_t i=0;i<nd;++i)shape.push_back(GDALDimensionGetSize(dims[i]));GDALReleaseDimensions(dims,nd);
    std::vector<GUInt64> start;std::vector<size_t> count;size_t n=1;
    for(size_t i=0;i<rank;++i){auto s=starts[static_cast<int>(i)].ToLong(),c=counts[static_cast<int>(i)].ToLong();
        require(s>=0&&c>0&&static_cast<uint64_t>(s)<=shape[i]&&static_cast<uint64_t>(c)<=shape[i]-s,"Slice is outside array");
        require(c<=INT_MAX && n<=static_cast<size_t>(INT_MAX)/c,"Slice exceeds bridge limit; read smaller windows");
        start.push_back(s);count.push_back(static_cast<size_t>(c));n*=static_cast<size_t>(c);
    }
    require(n<=SIZE_MAX/sizeof(double),"Slice exceeds addressable memory");
    auto values=a.GetArray("values");require(!write||values.Size()==n,"Slice values size does not match count");
    std::vector<double> data(n);if(write)for(size_t i=0;i<n;++i)data[i]=CPLAtof(values[static_cast<int>(i)].ToString().c_str());
    auto edt=GDALExtendedDataTypeCreate(GDT_Float64);
    int ok=write?GDALMDArrayWrite(h,start.data(),count.data(),nullptr,nullptr,edt,data.data(),data.data(),n*sizeof(double)):
        GDALMDArrayRead(h,start.data(),count.data(),nullptr,nullptr,edt,data.data(),data.data(),n*sizeof(double));
    GDALExtendedDataTypeRelease(edt);if(write)return value(ok!=0);
    require(ok!=0,"Multidimensional read failed");return numbers(data.data(),data.size());
}
#include "runtime-md.hpp"
#include "runtime-band.hpp"
#include "runtime-band-structures.hpp"
#include "runtime-vector.hpp"
#include "runtime-coordinates.hpp"
inline CPLJSONObject Runtime::execute(const CPLJSONObject& request){
    Runtime& r=*this;const auto type=request.GetString("type"),method=request.GetString("method");
    auto id=std::stoull(request.GetString("handle","0"));
    if(method=="close"){close(id);return nullValue();}
    auto a=request.GetObj("parameters");void* h=type=="GdalRuntime"?nullptr:get(id,type);
    // A mutation can replace native children while their Kotlin wrappers still exist.
    // Invalidate aliases after arguments have been resolved and the C call has finished.
    std::string invalidated;
    if(type=="Feature" && (method.rfind("setGeometry",0)==0 || method.rfind("setGeomField",0)==0))invalidated="Geometry";
    if(type=="Geometry" && (method=="empty" || method.rfind("removeGeometry",0)==0))invalidated="Geometry";
    if((type=="Geometry" && method.rfind("assignSpatialReference",0)==0) || (type=="GeomFieldDefn" && method.rfind("setSpatialRef",0)==0) || (type=="Dataset" && method.rfind("setGCPs",0)==0))invalidated="SpatialReference";
    if(type=="FeatureDefn" && method.rfind("setGeomType",0)==0)invalidated="GeomFieldDefn";
    if(type=="Band" && method.rfind("createMaskBand",0)==0)invalidated="Band";
    struct InvalidateAfter {
        Runtime& runtime;void* pointer;std::string type;bool overviews;
        ~InvalidateAfter(){if(!type.empty())runtime.invalidateChildren(pointer,type);if(overviews)runtime.invalidateOverviews(pointer);}
    } invalidate{r,h,invalidated,type=="Dataset" && (method.rfind("buildOverviews",0)==0 || method.rfind("createMaskBand",0)==0)};
#include "runtime.generated.inc"
    throw std::runtime_error("Unknown GDAL runtime operation: "+type+"."+method);
}
}

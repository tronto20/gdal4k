#pragma once

inline CPLJSONObject bandStatistics(GDALRasterBandH h,const CPLJSONObject& a,bool compute) {
    double v[4]={};
    auto code=compute?GDALComputeRasterStatistics(h,a.GetBool("approximate"),v,v+1,v+2,v+3,nullptr,nullptr):
        GDALGetRasterStatistics(h,a.GetBool("approximate"),a.GetBool("force"),v,v+1,v+2,v+3);
    if(!compute && !a.GetBool("force") && code==CE_Warning)return nullValue();
    require(code==CE_None,"Cannot obtain band statistics");return numbers(v,4);
}
inline CPLJSONObject setBandStatistics(GDALRasterBandH h,const CPLJSONObject& a) {
    auto lo=CPLAtof(a["minimum"].ToString().c_str()),hi=CPLAtof(a["maximum"].ToString().c_str());
    auto mean=CPLAtof(a["mean"].ToString().c_str()),sd=CPLAtof(a["standardDeviation"].ToString().c_str());
    require(std::isfinite(lo)&&std::isfinite(hi)&&std::isfinite(mean)&&std::isfinite(sd)&&lo<=hi&&sd>=0,"Invalid band statistics");
    return value(GDALSetRasterStatistics(h,lo,hi,mean,sd));
}
inline void histogramRange(double lo,double hi) {
    require(std::isfinite(lo)&&std::isfinite(hi)&&lo<hi&&std::isfinite(hi-lo),"Invalid histogram range");
}
inline CPLJSONObject bandHistogram(GDALRasterBandH h,const CPLJSONObject& a) {
    auto lo=CPLAtof(a["minimum"].ToString().c_str()),hi=CPLAtof(a["maximum"].ToString().c_str());histogramRange(lo,hi);
    auto n=a.GetInteger("buckets");require(n>0 && static_cast<uint64_t>(n)<=SIZE_MAX/sizeof(GUIntBig),"Invalid histogram bucket count");
    std::vector<GUIntBig> v(static_cast<size_t>(n));
    require(GDALGetRasterHistogramEx(h,lo,hi,n,v.data(),a.GetBool("includeOutOfRange"),a.GetBool("approximate"),nullptr,nullptr)==CE_None,"Cannot compute band histogram");
    return numbers(v.data(),v.size());
}
inline CPLJSONObject defaultBandHistogram(GDALRasterBandH h,bool force) {
    double lo=0,hi=0;int n=0;GUIntBig* values=nullptr;
    auto code=GDALGetDefaultHistogramEx(h,&lo,&hi,&n,&values,force,nullptr,nullptr);
    struct FreeHistogram {GUIntBig* p;~FreeHistogram(){CPLFree(p);}} cleanup{values};
    if(!force && code==CE_Warning)return nullValue();
    require(code==CE_None && n>0 && values,"Cannot obtain default band histogram");
    CPLJSONObject v;v.Add("minimum",value(lo)["value"]);v.Add("maximum",value(hi)["value"]);
    for(int i=0;i<n;++i)require(values[i]<=static_cast<GUIntBig>(INT64_MAX),"Histogram count exceeds signed Long");
    v.Add("counts",numbers(values,static_cast<size_t>(n))["value"]);return value(v);
}
inline CPLJSONObject setBandHistogram(GDALRasterBandH h,const CPLJSONObject& a) {
    auto lo=CPLAtof(a["minimum"].ToString().c_str()),hi=CPLAtof(a["maximum"].ToString().c_str());histogramRange(lo,hi);
    auto array=a.GetArray("counts");require(array.Size()>0 && static_cast<uint64_t>(array.Size())<=SIZE_MAX/sizeof(GUIntBig),"Invalid histogram bucket count");
    std::vector<GUIntBig> counts;counts.reserve(static_cast<size_t>(array.Size()));
    for(auto item:array){auto n=std::stoll(item.ToString());require(n>=0,"Histogram counts cannot be negative");counts.push_back(static_cast<GUIntBig>(n));}
    return value(GDALSetDefaultHistogramEx(h,lo,hi,static_cast<int>(counts.size()),counts.data()));
}

// PAM's NoData setters do not invalidate previously generated masks in GDAL 3.12.4.
// A protected base member pointer is applied to the real GDALRasterBand object:
// no downcast to a fictitious derived instance or layout-dependent access is used.
struct BandMaskCacheAccess : GDALRasterBand {
    static void invalidate(GDALRasterBandH handle) {
        auto member = &BandMaskCacheAccess::InvalidateMaskBand;
        static_assert(std::is_same_v<decltype(member), void (GDALRasterBand::*)()>);
        (GDALRasterBand::FromHandle(handle)->*member)();
    }
};
inline CPLJSONObject setBandNoData(Runtime& runtime, GDALRasterBandH band, const CPLJSONObject& args, const std::string& operation) {
    const auto type = GDALGetRasterDataType(band);
    if (operation == "setNoDataValueAsInt64") require(type == GDT_Int64, "Exact NoData type does not match band");
    if (operation == "setNoDataValueAsUInt64") require(type == GDT_UInt64, "Exact NoData type does not match band");
    auto oldMask = GDALGetMaskBand(band);
    auto nativeMask = GDALRasterBand::FromHandle(oldMask);
    const bool generated = dynamic_cast<GDALAllValidMaskBand*>(nativeMask) || dynamic_cast<GDALNoDataMaskBand*>(nativeMask);
    CPLErr code;
    if (operation == "setNoDataValueAsInt64") code = GDALSetRasterNoDataValueAsInt64(band, std::stoll(args["value"].ToString()));
    else if (operation == "setNoDataValueAsUInt64") code = GDALSetRasterNoDataValueAsUInt64(band, std::stoull(args["value"].ToString()));
    else if (operation == "deleteNoDataValue") code = GDALDeleteRasterNoDataValue(band);
    else code = GDALSetRasterNoDataValue(band, CPLAtof(args["value"].ToString().c_str()));
    // A driver may already have freed its old cached wrapper. Invalidate aliases by
    // pointer identity only, before resetting a remaining generated cache. Explicit
    // mask bands (including MEM-owned masks) retain their pixel data and ownership.
    if (oldMask) runtime.invalidatePointer(oldMask);
    if (generated) BandMaskCacheAccess::invalidate(band);
    return value(code);
}

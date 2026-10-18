#pragma once

inline std::string bandString(const CPLJSONObject& item) {
    auto s=item.ToString();require(s.find('\0')==std::string::npos,"NUL in GDAL string");return s;
}
inline GDALColorEntry colorEntry(const CPLJSONObject& item) {
    auto a=item.ToArray();require(a.Size()==4,"Color entry must have four components");
    short v[4];for(int i=0;i<4;++i){auto n=a[i].ToInteger();require(n>=INT16_MIN&&n<=INT16_MAX,"Color component exceeds Short");v[i]=static_cast<short>(n);}
    return {v[0],v[1],v[2],v[3]};
}
inline CPLJSONObject colorEntryResult(const GDALColorEntry* p) {
    if(!p)return nullValue();int v[4]={p->c1,p->c2,p->c3,p->c4};return numbers(v,4);
}
inline CPLJSONObject colorTableOperation(void* h,const CPLJSONObject& a,const std::string& op) {
    if(op=="getColorEntry"||op=="getColorEntryAsRGB") {
        int n=a.GetInteger("index");require(n>=0,"Negative color index");
        if(n>=GDALGetColorEntryCount(h))return nullValue();
        if(op=="getColorEntry")return colorEntryResult(GDALGetColorEntry(h,n));
        GDALColorEntry v{};return GDALGetColorEntryAsRGB(h,n,&v)?colorEntryResult(&v):nullValue();
    }
    if(op=="setColorEntry") {
        auto n=a.GetInteger("index");require(n>=0&&n<INT_MAX,"Invalid color index");auto entry=colorEntry(a["entry"]);
        GDALSetColorEntry(h,n,&entry);return nullValue();
    }
    auto start=a.GetInteger("startIndex"),end=a.GetInteger("endIndex");
    require(start>=0&&end>=start&&end<INT_MAX,"Invalid color ramp range");
    auto from=colorEntry(a["start"]),to=colorEntry(a["end"]);GDALCreateColorRamp(h,start,&from,end,&to);return nullValue();
}
inline void ratColumn(void* h,int c) {require(c>=0&&c<GDALRATGetColumnCount(h),"RAT column is outside table");}
inline void ratWindow(void* h,int c,int start,int count) {
    ratColumn(h,c);require(start>=0&&count>=0&&static_cast<int64_t>(start)+count<=GDALRATGetRowCount(h),"RAT row window is outside table");
}
inline CPLJSONObject ratValues(void* h,const CPLJSONObject& a,const std::string& op) {
    bool write=op.rfind("write",0)==0;int col=a.GetInteger("column"),start=a.GetInteger("startRow");
    auto array=a.GetArray("values");int count=write?array.Size():a.GetInteger("count");ratWindow(h,col,start,count);
    auto expected=op.find("String")!=std::string::npos?GFT_String:op.find("Integer")!=std::string::npos?GFT_Integer:GFT_Real;
    require(GDALRATGetTypeOfCol(h,col)==expected,"RAT value method does not match column type");
    if(op.find("String")!=std::string::npos) {
        if(write) {std::vector<std::string> values;values.reserve(count);for(auto item:array)values.push_back(bandString(item));
            for(int i=0;i<count;++i)GDALRATSetValueAsString(h,start+i,col,values[i].c_str());return nullValue();}
        CPLJSONArray values;for(int i=0;i<count;++i)values.Add(GDALRATGetValueAsString(h,start+i,col));return value(values);
    }
    auto flag=write?GF_Write:GF_Read;CPLErr code=CE_None;
    if(op.find("Integer")!=std::string::npos) {
        require(static_cast<uint64_t>(count)<=SIZE_MAX/sizeof(int),"RAT buffer exceeds addressable memory");
        std::vector<int> v(static_cast<size_t>(count));if(write)for(int i=0;i<count;++i)v[i]=array[i].ToInteger();
        if(count)code=GDALRATValuesIOAsInteger(h,flag,col,start,count,v.data());
        if(write)return value(code);require(code==CE_None,"Cannot read RAT values");return numbers(v.data(),v.size());
    }
    require(static_cast<uint64_t>(count)<=SIZE_MAX/sizeof(double),"RAT buffer exceeds addressable memory");
    std::vector<double> v(static_cast<size_t>(count));if(write)for(int i=0;i<count;++i)v[i]=CPLAtof(array[i].ToString().c_str());
    if(count)code=GDALRATValuesIOAsDouble(h,flag,col,start,count,v.data());
    if(write)return value(code);require(code==CE_None,"Cannot read RAT values");return numbers(v.data(),v.size());
}
inline CPLJSONObject ratScalar(void* h,const CPLJSONObject& a,const std::string& op) {
    int row=a.GetInteger("row"),col=a.GetInteger("column");ratWindow(h,col,row,1);
    auto expected=op.find("String")!=std::string::npos?GFT_String:op.find("Int")!=std::string::npos?GFT_Integer:GFT_Real;
    require(GDALRATGetTypeOfCol(h,col)==expected,"RAT value method does not match column type");
    if(op=="getValueAsInt")return value(GDALRATGetValueAsInt(h,row,col));
    if(op=="getValueAsDouble")return value(GDALRATGetValueAsDouble(h,row,col));
    if(op=="getValueAsString")return value(GDALRATGetValueAsString(h,row,col));
    if(op=="setValueAsInt")GDALRATSetValueAsInt(h,row,col,a.GetInteger("value"));
    else if(op=="setValueAsDouble")GDALRATSetValueAsDouble(h,row,col,CPLAtof(a["value"].ToString().c_str()));
    else {auto s=bandString(a["value"]);GDALRATSetValueAsString(h,row,col,s.c_str());}
    return nullValue();
}
struct BandBlock {
    int x,y,width,height,validWidth=0,validHeight=0,bytes;size_t size;
    BandBlock(void* h,const CPLJSONObject& a):x(a.GetInteger("blockX")),y(a.GetInteger("blockY")) {
        GDALGetBlockSize(h,&width,&height);bytes=GDALGetDataTypeSizeBytes(GDALGetRasterDataType(h));
        require(width>0&&height>0&&bytes>0,"Invalid native block format");
        require(x>=0&&y>=0&&static_cast<int64_t>(x)*width<GDALGetRasterBandXSize(h)&&static_cast<int64_t>(y)*height<GDALGetRasterBandYSize(h),"Block is outside band");
        require(GDALGetActualBlockSize(h,x,y,&validWidth,&validHeight)==CE_None,"Cannot obtain actual block size");
        require(static_cast<uint64_t>(width)*height<=SIZE_MAX/static_cast<size_t>(bytes),"Block exceeds addressable memory");
        size=static_cast<size_t>(width)*height*bytes;require(size<=INT_MAX,"Block exceeds Kotlin array size");
    }
};
inline CPLJSONObject bandBlock(void* h,const CPLJSONObject& a,bool write) {
    BandBlock block(h,a);auto array=a.GetArray("buffer");auto count=write&&!activeBinary?array.Size():a.GetInteger("bufferSize");
    require(count>=0&&static_cast<size_t>(count)>=block.size,"Buffer is smaller than native block");
    validateBinary(block.size,1,write);
    std::vector<GByte> data(block.size,0);if(write&&activeBinary)binaryInput(data.data(),block.size);else if(write)for(size_t i=0;i<block.size;++i)data[i]=static_cast<GByte>(array[static_cast<int>(i)].ToInteger());
    auto code=GDALFlushRasterCache(h);
    if(code==CE_None&&write)code=GDALDropRasterCache(h);
    if(code==CE_None)code=write?GDALWriteBlock(h,block.x,block.y,data.data()):GDALReadBlock(h,block.x,block.y,data.data());
    if(!write&&code==CE_None)for(int row=0;row<block.height;++row) {
        auto start=(static_cast<size_t>(row)*block.width+(row<block.validHeight?block.validWidth:0))*block.bytes;
        auto end=static_cast<size_t>(row+1)*block.width*block.bytes;std::fill(data.begin()+start,data.begin()+end,0);
    }
    CPLJSONObject out;out.Add("code",code);if(!write&&code==CE_None){if(activeBinary)binaryOutput(data.data(),data.size());else out.Add("buffer",numbers(data.data(),data.size())["value"]);}
    CPLJSONObject result;result.Add("value",out);return result;
}

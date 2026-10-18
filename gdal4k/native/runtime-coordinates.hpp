#pragma once
// Included within g4k after the common JSON and binary helpers.
inline CPLJSONObject transformCoordinatePoints(OGRCoordinateTransformationH h,const CPLJSONObject& a) {
    std::vector<double> records;
    size_t count=0;
    if(activeBinary) {
        const auto requested=a.GetLong("pointCount");
        require(requested>=0 && requested<=std::numeric_limits<int>::max()/40,"Invalid coordinate point count");
        count=static_cast<size_t>(requested);
        require(activeBinary->length==count*40,"Invalid binary coordinate input length");
        validateBinary(count*5,sizeof(double),true);records.resize(count*5);binaryInput(records.data(),records.size());
    } else {
        const auto points=a.GetArray("points");count=static_cast<size_t>(points.Size());
        require(count<=std::numeric_limits<size_t>::max()/5,"Coordinate count overflow");records.resize(count*5);
        size_t i=0;for(auto point:points) {
            auto v=doubles(point.ToArray());require(v.size()>=2 && v.size()<=4,"A point must have two, three or four coordinates");
            records[i*5]=static_cast<double>(v.size());std::copy(v.begin(),v.end(),records.begin()+i*5+1);++i;
        }
    }
    // Validate the entire payload before GDAL work, including mixed 2D/3D/4D batches.
    for(size_t i=0;i<count;++i)require(records[i*5]==2 || records[i*5]==3 || records[i*5]==4,"Invalid coordinate dimensions");
    constexpr size_t chunk=1024;
    std::vector<double> x(chunk),y(chunk),z(chunk),t(chunk);std::vector<int> success(chunk);
    for(size_t first=0;first<count;) {
        binaryCheckpoint();
        const auto dimensions=static_cast<int>(records[first*5]);size_t n=1;
        while(n<chunk && first+n<count && records[(first+n)*5]==dimensions)++n;
        for(size_t j=0;j<n;++j) {const auto offset=(first+j)*5;x[j]=records[offset+1];y[j]=records[offset+2];z[j]=dimensions>2?records[offset+3]:0;t[j]=records[offset+4];success[j]=0;}
        const auto ok=dimensions==4?OCTTransform4D(h,static_cast<int>(n),x.data(),y.data(),z.data(),t.data(),success.data()):OCTTransformEx(h,static_cast<int>(n),x.data(),y.data(),z.data(),success.data());
        require(ok!=0 && std::all_of(success.begin(),success.begin()+n,[](int v){return v!=0;}),"Coordinate transformation failed");
        for(size_t j=0;j<n;++j) {const auto offset=(first+j)*5;records[offset+1]=x[j];records[offset+2]=y[j];if(dimensions>2)records[offset+3]=z[j];if(dimensions==4)records[offset+4]=t[j];}
        first+=n;
    }
    binaryCheckpoint();
    if(activeBinary) {binaryOutput(records.data(),records.size());return value(static_cast<int>(count));}
    CPLJSONArray out;for(size_t i=0;i<count;++i)out.Add(numbers(records.data()+i*5+1,static_cast<size_t>(records[i*5]))["value"]);return value(out);
}

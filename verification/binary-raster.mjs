// Self-contained: the same protocol and fixed MEM workload run in Node and Chromium.
export async function exerciseBinaryRaster(bridge) {
    if (![1,2,3,4,5,6].includes(bridge.binaryVersion)) throw new Error('Published engine lacks binary raster capability');
    const request=(type,handle,method,parameters={})=>JSON.stringify({op:'runtime',args:{type,handle,method,parameters}});
    async function call(type,handle,method,parameters={}) {
        const text=await bridge.call(request(type,handle,method,parameters));const reply=JSON.parse(text);
        if(reply.error) throw new Error(reply.error);
        return reply.result.value;
    }
    const driver=await call('GdalRuntime','0','getDriver_String',{name:'MEM'});
    const dataset=await call('Driver',driver,'create_String_Int_Int_Int_Int_List_String',{name:'',xSize:512,ySize:512,bandCount:1,dataType:7,options:[]});
    const count=512*512, source=new Float64Array(count);
    for(let i=0;i<count;i++)source[i]=(i%10007-5000)*0.125;
    const options={xOffset:0,yOffset:0,xSize:0,ySize:0,bufferWidth:0,bufferHeight:0,bands:[],resampling:0};
    const trials=[];
    async function roundtrip(binary) {
        if(!binary) {
            const code=await call('Dataset',dataset,'writeRaster_DoubleArray_RasterIOOptions',{options,buffer:Array.from(source)});
            if(code.code!==0)throw new Error('Legacy raster write failed');
            const read=await call('Dataset',dataset,'readRaster_DoubleArray_RasterIOOptions',{options,bufferSize:count});
            if(read.code!==0||read.buffer.length!==count)throw new Error('Legacy raster read failed');
            for(let i=0;i<count;i++)if(read.buffer[i]!==source[i])throw new Error('Legacy raster mismatch');
        } else {
            const input=new Uint8Array(source.buffer.slice(0));
            const written=await bridge.callBinary(request('Dataset',dataset,'writeRaster_DoubleArray_RasterIOOptions',{options,bufferSize:count}),input);
            if(input.byteLength!==0||source.byteLength!==count*8)throw new Error('Transfer detached caller storage or copied instead of transferring staging');
            if(JSON.parse(written.json).result?.value?.code!==0)throw new Error('Binary raster write failed: '+written.json);
            const read=await bridge.callBinary(request('Dataset',dataset,'readRaster_DoubleArray_RasterIOOptions',{options,bufferSize:count}),null);
            const metadata=JSON.parse(read.json);
            if(metadata.result?.value?.code!==0||read.bytes.byteLength!==count*8||read.json.length>512||'buffer' in metadata.result.value)throw new Error('Invalid binary raster response');
            const view=new DataView(read.bytes);
            for(let i=0;i<count;i++)if(view.getFloat64(i*8,true)!==source[i])throw new Error('Binary raster mismatch');
            return read.json.length;
        }
    }
    try {
        await roundtrip(false);await roundtrip(true);
        for(let trial=0;trial<3;trial++)for(const binary of trial%2?[true,false]:[false,true]) {
            const start=performance.now();let jsonChars=0;
            for(let i=0;i<3;i++)jsonChars=await roundtrip(binary)||0;
            trials.push({protocol:binary?'binary':'json',trial,elapsedMillis:performance.now()-start,roundTrips:3,samples:count,sampleBytes:8,readMetadataChars:jsonChars});
        }
        const bad=await bridge.callBinary(request('Dataset',dataset,'writeRaster_DoubleArray_RasterIOOptions',{options,bufferSize:count}),new Uint8Array(1));
        if(!JSON.parse(bad.json).error)throw new Error('Truncated binary buffer was accepted');
        await roundtrip(true); // A protocol error must not poison the context or retain buffers.
        return {result:'passed',trials,callerStoragePreserved:true,privateInputTransferred:true,truncatedInputRejected:true};
    } finally {await call('Handle',dataset,'close');await call('Handle',driver,'close');}
}

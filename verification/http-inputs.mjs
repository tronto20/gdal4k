// Real GDAL Worker integration; the Python harness extracts only published KLIB modules.
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const require=createRequire(import.meta.url);
const {chromium}=require(config.root+'/verification/node_modules/playwright');
async function exercise(createGdalWorker, url, host) {
    const bridge=await createGdalWorker();
    const base=url+'/policy/'+host+'/';
    const policy={requestTimeoutMillis:5000,maxRetries:0,retryDelayMillis:0,maxRetryDelayMillis:100,requireStrongETag:true};
    async function call(type,handle,method,parameters={},cancelled) {
        let response;
        if (/^read(Raster|Block)_/.test(method)) {
            if (![1,2,3,4].includes(bridge.binaryVersion)) throw new Error('HTTP regression requires binary raster capability');
            parameters={...parameters,bufferSize:parameters.buffer?.length??parameters.bufferSize};delete parameters.buffer;
            const packet=await bridge.callBinary(JSON.stringify({op:'runtime',args:{type,handle,method,parameters}}),null,()=>{},cancelled??(()=>false));
            response=JSON.parse(packet.json);
            if(response.result?.value?.code===0)response.result.value.buffer=Array.from(new Uint8Array(packet.bytes));
        } else response=JSON.parse(await bridge.call(JSON.stringify({op:'runtime',args:{type,handle,method,parameters}}),null,()=>{},method==='close'?undefined:cancelled??(()=>false)));
        if(response.error)throw new Error(response.error);
        return response.result.value;
    }
    const open=(name,options={},cancelled)=>call('GdalRuntime','0','openDataset_String_DatasetOpenOptions',{source:base+name+'.tif',options:{kind:2,options:[],httpInputPolicy:{...policy,...options}}},cancelled);
    const close=handle=>call('Handle',handle,'close');
    const band=dataset=>call('Dataset',dataset,'getRasterBand_Int',{index:1});
    async function read(handle,x,y,value,cancelled) {
        const raster=await call('Band',handle,'readRaster_Int_Int_Int_Int_ByteArray',{xOffset:x,yOffset:y,xSize:2,ySize:2,buffer:[0,0,0,0]},cancelled);
        if(raster.code!==0||raster.buffer.some(n=>(n&255)!==value))throw new Error('HTTP input pixel mismatch '+JSON.stringify(raster));
    }
    async function expectFailure(action,pattern) {
        let failure;
        try{await action();}catch(error){failure=error;}
        if(!failure||!pattern.test(String(failure)))throw new Error('Expected '+pattern+', received '+String(failure));
    }
    async function cancelSoon(action) {
        let cancelled=false;
        const timer=setTimeout(()=>{cancelled=true;},80),start=Date.now();
        try{await expectFailure(()=>action(()=>cancelled),/cancel|abort|interrupt/i);}
        finally{clearTimeout(timer);}
        if(Date.now()-start>2500)throw new Error('HTTP cancellation waited for request deadline');
    }
    const mode=async(name,value)=>{const response=await fetch(url+'/control?key='+encodeURIComponent('/policy/'+host+'/'+name+'.tif')+'&mode='+value);if(!response.ok)throw new Error('Fixture control failed');};
    try {
        for(const [name,options] of [['healthy',{}],['retry',{maxRetries:1}],['body-timeout',{requestTimeoutMillis:80,maxRetries:1}]]) {
            const dataset=await open(name,options);
            try{const b=await band(dataset);await read(b,0,0,42);await read(b,16382,16382,255);}finally{await close(dataset);}
        }
        const input=await open('healthy');
        let vrt;
        try {
            vrt=await call('GdalRuntime','0','buildVRT_List_Dataset_List_String',{sources:[{type:'Dataset',handle:input}],arguments:[]});
            await close(input);
            // Native VRT leases must retain the HTTP mount after the public source closes.
            await read(await band(vrt),16382,16382,255);
        }finally{if(vrt)await close(vrt);else await close(input);}
        await expectFailure(()=>open('permanent',{maxRetries:3}),/401/);
        await expectFailure(()=>open('full',{maxRetries:3}),/206|range/i);
        await cancelSoon(cancelled=>open('stall-open',{},cancelled));
        await cancelSoon(cancelled=>open('backoff',{maxRetries:3,retryDelayMillis:2000,maxRetryDelayMillis:3000},cancelled));
        const dataset=await open('read-cancel');
        try {
            const b=await band(dataset);await read(b,0,0,42);
            await mode('read-cancel','stall');
            // readBlock has no GDAL progress callback; fetch cancellation must still bypass the call queue.
            await cancelSoon(cancelled=>call('Band',b,'readBlock_Int_Int_ByteArray',{blockX:63,blockY:63,bufferSize:65536},cancelled));
            await mode('read-cancel','healthy');
            await read(b,16382,16382,255); // Cancel the operation, preserve the caller's provider/Dataset.
        }finally{await close(dataset);}
        const changed=await open('changed');
        try {
            const b=await band(changed);await read(b,0,0,42);await mode('changed','changed');
            await expectFailure(()=>read(b,16382,16382,255),/changed/i);
            await mode('changed','healthy');
            await expectFailure(()=>read(b,8192,8192,0),/changed/i);
        }finally{await close(changed);}
        const reopened=await open('changed');try{await read(await band(reopened),16382,16382,255);}finally{await close(reopened);}
        const final=await open('healthy');try{await read(await band(final),0,0,42);}finally{await close(final);}
    }finally{await bridge.close();}
    console.log('GDAL4K_HTTP_BINARY_RASTER_OK',host);
    return 'passed';
}
const {createGdalWorker}=await import(pathToFileURL(config.assets+'/index.mjs'));
await exercise(createGdalWorker,config.url,'node');
const options={headless:true,args:process.platform==='linux'?['--no-sandbox']:[]};
if(process.env.CHROME_BIN)options.executablePath=process.env.CHROME_BIN;
const browser=await chromium.launch(options);
try {
    const page=await browser.newPage();await page.goto(config.url);
    await page.evaluate(async({url,exercise})=>{
        const {createGdalWorker}=await import(new URL('/assets/index.mjs',location.href).href);
        if(globalThis.crossOriginIsolated)throw new Error('Unexpected isolation requirement');
        return (0,eval)('('+exercise+')')(createGdalWorker,url,'browser');
    },{url:config.url,exercise:exercise.toString()});
}finally{await browser.close();}

import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {exerciseResources} from './web-resources.mjs';
import {exerciseBinaryRaster} from './binary-raster.mjs';
const binaryResults={};
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const require=createRequire(import.meta.url);
const {chromium}=require(config.root+'/verification/node_modules/playwright');
async function exercise(bridge, source, expectedFirst=42) {
    async function call(type,handle,method,parameters={}) {
        const response=JSON.parse(await bridge.call(JSON.stringify({op:'runtime',args:{type,handle,method,parameters}})));
        if(response.error)throw new Error(response.error);
        return response.result.value;
    }
    const dataset=await call('GdalRuntime','0','openDataset_String_DatasetOpenOptions',{source,options:{update:false,kind:2,options:[]}});
    const band=await call('Dataset',dataset,'getRasterBand_Int',{index:1});
    for(const [x,y,value] of [[0,0,expectedFirst],[16382,16382,255]]) {
        const raster=await call('Band',band,'readRaster_Int_Int_Int_Int_ByteArray',{xOffset:x,yOffset:y,xSize:2,ySize:2,buffer:[0,0,0,0]});
        if(raster.code!==0 || raster.buffer.some(n=>(n&255)!==value))throw new Error('Wrong direct-input pixels: '+JSON.stringify(raster));
    }
    const files=await call('Dataset',dataset,'translate_String_String_List_String',{driver:'GTiff',name:'window.tif',arguments:['-srcwin','0','0','2','2','-co','COMPRESS=JXL','-co','JXL_LOSSLESS=YES']});
    const converted=await call('GdalRuntime','0','openDataset_GdalFiles_DatasetOpenOptions',{files,options:{update:false,kind:2,options:[]}});
    const outputBand=await call('Dataset',converted,'getRasterBand_Int',{index:1});
    const pixels=await call('Band',outputBand,'readRaster_ByteArray',{buffer:[0,0,0,0]});
    if(pixels.code!==0 || pixels.buffer.some(n=>(n&255)!==expectedFirst))throw new Error('Direct-input JXL conversion failed');
    await call('Handle',converted,'close');
    await call('Handle',dataset,'close');
    return {call,dataset};
}
if(!config.browserOnly){
    const {createGdalWorker}=await import(pathToFileURL(config.assets+'/index.mjs'));
    const bridge=await createGdalWorker();
    const {Inputs}=await import(pathToFileURL(config.assets+'/gdal4k-inputs.mjs'));
    const inputs=new Inputs();const mounted=await inputs.mount(config.file);
    for(let i=0;i<140;i++)await inputs.read(mounted.path,65536+i*65536,65536);
    if(inputs.cachedBytes>8*1024*1024)throw new Error('Input cache exceeded its bound');
    mounted.close();
    if(inputs.cachedBytes!==0 || inputs.providers.size!==0)throw new Error('Input cache survived close');
    try {
        await exerciseResources(bridge);
        binaryResults.Node=await exerciseBinaryRaster(bridge);
        const bytes=[...Buffer.from(config.zip,'base64')];
        async function call(type,handle,method,parameters={}) {
            const response=JSON.parse(await bridge.call(JSON.stringify({op:'runtime',args:{type,handle,method,parameters}})));
            if(response.error)throw new Error(response.error);
            return response.result.value;
        }
        await call('GdalRuntime','0','writeMemoryFile_String_ByteArray',{path:'/vsimem/input.zip',bytes});
        const zipped=await call('GdalRuntime','0','openDataset_String_DatasetOpenOptions',{source:'/vsizip//vsimem/input.zip/small.tif',options:{update:false,kind:2,options:[]}});
        if(await call('Dataset',zipped,'getRasterXSize')!==3)throw new Error('Existing GDAL VSI path support regressed');
        await call('Handle',zipped,'close');
        await call('GdalRuntime','0','deleteMemoryFile_String',{path:'/vsimem/input.zip'});
        await exercise(bridge,config.file);
        await exercise(bridge,config.url+'/large.tif');
        for(const path of ['/no-range.tif','/changed.tif','/wrong-range.tif','/missing.tif']) {
            let failed=false;
            try{await exercise(bridge,config.url+path);}catch{failed=true;}
            if(!failed)throw new Error('Expected HTTP input failure: '+path);
        }
        await exercise(bridge,config.file); // Errors must not poison later GDAL calls.
    }finally{await bridge.close();}
}
const options={headless:true,args:process.platform==='linux'?['--no-sandbox']:[]};
if(process.env.CHROME_BIN)options.executablePath=process.env.CHROME_BIN;
const browser=await chromium.launch(options);
try{
    const page=await browser.newPage();
    await page.goto(config.url.replace("127.0.0.1","localhost"));
    await page.locator('#file').setInputFiles(config.file);
    // The engine runs without COOP/COEP, SharedArrayBuffer, or Wasm GC.
    binaryResults.Chromium=await page.evaluate(async({url,exercise,resourceExercise,binaryExercise})=>{
        const {createGdalWorker}=await import(new URL('/assets/index.mjs', location.href).href);
        const bridge=await createGdalWorker();
        const run=(0,eval)('('+exercise+')');
        try {
            await (0,eval)('('+resourceExercise+')')(bridge);
            if(globalThis.crossOriginIsolated)throw new Error('Test accidentally requires cross-origin isolation');
            await run(bridge,url+'/large.tif');
            const file=document.querySelector('#file').files[0];
            const selected={call:request=>bridge.call(request,file)};
            // Attach the File only to dataset opening, not to every GDAL call.
            const once={call:request=>JSON.parse(request).args.method==='openDataset_String_DatasetOpenOptions'?selected.call(request):bridge.call(request)};
            await run(once,file.name);
            for(const path of ['/no-range.tif','/changed.tif','/wrong-range.tif','/missing.tif','/no-cors.tif','/no-expose.tif']) {
                let failed=false;
                try{await run(bridge,url+path);}catch{failed=true;}
                if(!failed)throw new Error('Expected HTTP input failure: '+path);
            }
            await run(bridge,url+'/large.tif');
            return await (0,eval)('('+binaryExercise+')')(bridge);
        }finally{await bridge.close();}
    },{url:config.url,exercise:exercise.toString(),resourceExercise:exerciseResources.toString(),binaryExercise:exerciseBinaryRaster.toString()});
}finally{await browser.close();}

fs.writeFileSync(config.binaryOutput,JSON.stringify(binaryResults,null,2));

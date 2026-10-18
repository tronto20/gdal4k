// Diagnostic for the pinned Kotlin/JS compiler's friend exports, not a public runtime API.
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const root=path.resolve(__dirname,'..');
const stage=process.argv[2];
if(!['before','after'].includes(stage))throw new Error('Choose before or after');
process.env.NODE_PATH=[path.join(root,'build/js/node_modules'),process.env.NODE_PATH].filter(Boolean).join(path.delimiter);
require('node:module').Module._initPaths();
const directory=path.join(root,'gdal4k-runtime/build/compileSync/js/test/testDevelopmentExecutable/kotlin');
const runtimePath=path.join(directory,'gdal4k-gdal4k-runtime.js');
const stdlibPath=path.join(directory,'kotlin-kotlin-stdlib.js');
function linkedExport(file,name) {
    const source=fs.readFileSync(file,'utf8');
    const match=source.match(new RegExp(String.raw`_\.\$_\$\.([\w$]+) = `+name+';'));
    if(!match)throw new Error('Pinned Kotlin friend export missing: '+name);
    return require(file).$_$[match[1]];
}
const pack=linkedExport(runtimePath,'packRaster'),unpack=linkedExport(runtimePath,'unpackRaster');
const empty=linkedExport(stdlibPath,'EmptyCoroutineContext_getInstance');
const suspended=linkedExport(stdlibPath,'get_COROUTINE_SUSPENDED');
const Long=linkedExport(stdlibPath,'Long');
const low=Object.getOwnPropertyNames(Long.prototype).find(name=>name.startsWith('get_low_'));
const high=Object.getOwnPropertyNames(Long.prototype).find(name=>name.startsWith('get_high_'));
if(!low||!high)throw new Error('Pinned Long accessors missing');
function call(fn,args) {
    return new Promise((resolve,reject)=>{
        const resume=value=>value?.exception_1?reject(value.exception_1):resolve(value);
        const continuation={get_context_h02k06_k$:()=>empty(),resumeWith_b9cu3x_k$:resume,resumeWith_dtxwbr_k$:resume};
        try {const value=fn(...args,continuation);if(value!==suspended())resolve(value);}catch(error){reject(error);}
    });
}
(async()=>{
    const trials=[],size=1048576;
    for(const kind of ['Float64','Int64']) {
        const input=kind==='Float64'?new Float64Array(size):Array.from({length:size},(_,i)=>new Long(i-5000,i%2?-1:0x1fffff));
        const output=kind==='Float64'?new Float64Array(size):Array(size).fill(new Long(0,0));
        if(kind==='Int64'){input.$type$='LongArray';output.$type$='LongArray';}
        else for(let i=0;i<size;i++)input[i]=(i%10007-5000)*0.125;
        for(let trial=-1;trial<3;trial++) {
            const start=performance.now();const bytes=await call(pack,[input]);const packed=performance.now();
            await call(unpack,[bytes,output]);const end=performance.now();
            if(bytes.byteLength!==size*8)throw new Error('Wrong codec length');
            for(let i=0;i<size;i++) {
                if(kind==='Float64'?output[i]!==input[i]:output[i][low]()!==input[i][low]()||output[i][high]()!==input[i][high]())throw new Error('Wrong codec bits');
            }
            if(trial>=0)trials.push({kind,trial,samples:size,bytes:size*8,packMillis:packed-start,unpackMillis:end-packed});
        }
    }
    const sha=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
    const result={result:'passed',stage,node:process.version,trials,compiledRuntimeSha256:sha(runtimePath),compiledStdlibSha256:sha(stdlibPath),scope:'Existing compiled Kotlin/JS codec in EmptyCoroutineContext. Excludes real dispatcher yielding, transport, Worker and GDAL. Diagnostic uses pinned compiler friend exports; production code does not.'};
    const output=path.join(root,'verification/results/js-codec-benchmark-'+stage+'.json');fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,JSON.stringify(result,null,2)+'\n');
    console.log(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exitCode=1;});

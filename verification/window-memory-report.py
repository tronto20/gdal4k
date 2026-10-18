#!/usr/bin/env python3
"""Compare real full/windowed GDAL reads in isolated JVMs; report speed/allocation/OS peak RSS.

macOS /usr/bin/time reports bytes; Linux GNU time reports KiB. This measures the
whole Java process (JVM, GDAL, caches and operation), not just Kotlin allocations.
Results are diagnostic evidence, not performance pass/fail thresholds.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import time

ROOT = Path(__file__).resolve().parent.parent

def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): digest.update(block)
    return digest.hexdigest()

def peak_rss_bytes(text, system):
    if system == 'Darwin':
        match = re.search(r'^\s*(\d+)\s+maximum resident set size', text, re.M)
        multiplier = 1
    elif system == 'Linux':
        match = re.search(r'Maximum resident set size \(kbytes\):\s*(\d+)', text)
        multiplier = 1024
    else:
        raise ValueError('Unsupported RSS host: ' + system)
    if match is None or int(match[1]) <= 0:
        raise ValueError('Missing positive OS peak RSS')
    return int(match[1]) * multiplier

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-compile',action='store_true')
    parser.add_argument('--target',default='macos-arm64' if platform.system()=='Darwin' else 'linux-amd64')
    args=parser.parse_args()
    system=platform.system()
    if system not in ('Darwin','Linux'):
        raise SystemExit('OS peak RSS probe supports macOS and Linux only; no fabricated memory result for this host')
    timer=Path('/usr/bin/time')
    if not timer.is_file():raise SystemExit('Missing OS time executable')
    if not args.skip_compile:
        subprocess.run([str(ROOT/'gradlew'),'--console=plain',':gdal4k-runtime:writeWindowMemoryClasspath','-I',str(ROOT/'verification/window-memory.init.gradle'),'--no-configuration-cache'],cwd=ROOT,check=True)
    cp=json.loads((ROOT/'gdal4k-runtime/build/window-memory-classpath.json').read_text())
    compiled=list((ROOT/'gdal4k-runtime/build/classes/kotlin/jvm/test/dev/gdal4k').glob('RasterWindowMemoryProbe*.class'))
    if not compiled:raise RuntimeError('Compile the probe first and do not run it concurrently with Gradle compilation')
    # Every measurement uses identical JVM/cache settings; these are probe settings only.
    env=os.environ.copy();env['GDAL_CACHEMAX']='16';env['LC_ALL']='C'
    command=[cp['java'],'-Xms32m','-Xmx1536m','-XX:+UseG1GC','-cp',cp['classpath'],'dev.gdal4k.RasterWindowMemoryProbe']
    output=ROOT/'verification/results/window-memory'/datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True,exist_ok=False)
    fixture=output/'input.tif'
    subprocess.run([*command,'fixture',str(fixture)],cwd=ROOT,env=env,check=True,stdout=(output/'fixture.log').open('w'),stderr=subprocess.STDOUT)
    fixture_sha=sha(fixture)
    records=[]
    try:
        for trial,order in enumerate([['full','allocate','reuse'],['reuse','full','allocate'],['allocate','reuse','full']]):
            for mode in order:
                # Prime the same OS file pages before each fresh JVM, outside its RSS/time.
                with fixture.open('rb') as stream:
                    while stream.read(1024*1024):pass
                prefix=output/(str(trial)+'-'+mode);rss=prefix.with_suffix('.time.txt')
                timer_args=['-l'] if system=='Darwin' else ['-v']
                result=subprocess.run([str(timer),*timer_args,'-o',str(rss),*command,mode,str(fixture)],cwd=ROOT,env=env,text=True,capture_output=True,timeout=300)
                prefix.with_suffix('.log').write_text(result.stdout+result.stderr)
                if result.returncode:raise RuntimeError('Probe failed: '+str(prefix)+': '+result.stderr[-1000:])
                matches=re.findall(r'^GDAL4K_WINDOW_MEMORY (.+)$',result.stdout,re.M)
                if len(matches)!=1:raise RuntimeError('Missing unique probe JSON: '+str(prefix))
                record=json.loads(matches[0]);record['trial']=trial
                usage=rss.read_text()
                record['peakRssBytes']=peak_rss_bytes(usage,system)
                assert record['mode']==mode and record['samples']==2048*2048*3 and record['checksum']==record['samples']*1.25
                record['log']=str(prefix.with_suffix('.log').relative_to(ROOT));record['rssLog']=str(rss.relative_to(ROOT))
                record['logSha256']=sha(prefix.with_suffix('.log'));record['rssLogSha256']=sha(rss);records.append(record)
                print(json.dumps(record),flush=True)
        assert sha(fixture)==fixture_sha
        manifest=ROOT/'gdal4k-engine/build/engine'/args.target/'manifest.json';engine=json.loads(manifest.read_text());assert engine['target']==args.target
        for path,digest in engine['artifacts'].items():assert sha(ROOT/'gdal4k-engine/build'/path)==digest,path
        median={mode:{key:statistics.median(r[key] for r in records if r['mode']==mode) for key in ['elapsedNanos','jvmAllocatedBytes','peakRssBytes','sampledPeakHeapUsedBytes']} for mode in ['full','allocate','reuse']}
        proof={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'platform':platform.platform(),'target':args.target,'records':records,'median':median,'fixture':{'bytes':fixture.stat().st_size,'sha256':fixture_sha,'width':2048,'height':2048,'type':'Float64','driver':'GTiff','compression':'NONE','tiled':256},'settings':{'jvm':['-Xms32m','-Xmx1536m','-XX:+UseG1GC'],'GDAL_CACHEMAX':'16 MiB benchmark only','rounds':3,'trials':3},'sourceSha256':{str(path.relative_to(ROOT)):sha(path) for path in [Path(__file__).resolve(),ROOT/'verification/window-memory.init.gradle',ROOT/'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/RasterWindowMemoryProbe.kt',ROOT/'gdal4k/src/commonMain/kotlin/dev/gdal4k/runtime/RasterWindows.kt']},'compiledProbeSha256':{str(p.relative_to(ROOT)):sha(p) for p in compiled},'compiledApiSha256':sha(ROOT/'gdal4k/build/classes/kotlin/jvm/main/dev/gdal4k/runtime/RasterWindowsKt.class'),'engineFingerprint':engine['source_fingerprint'],'engineManifestSha256':sha(manifest),'scope':'Nine fresh JVMs, alternating order; same primed uncompressed GTiff, small tile warmup, checksum, three scan rounds. Elapsed operation time includes reads and identical checksum computation. JVM bytes are cumulative all-thread allocations. JVM heap used is sampled every2ms during the operation and can miss brief peaks; it is not OS RSS or exact heap high water. The sampler runs in every mode and contributes small measurement overhead. Peak RSS is OS high-water memory for entire child JVM including JVM/GDAL/caches/startup/warmup/cleanup, not a subtraction or native-only allocation measure. OS cache is primed by parent before each probe. Native/JS/Wasm/Android performance and cold disk I/O are not measured. No timing/RSS pass threshold or zero-copy claim.','logsDirectory':str(output.relative_to(ROOT))}
        report=ROOT/'verification/results'/('window-memory-'+args.target+'.json');report.write_text(json.dumps(proof,indent=2)+'\n');print(json.dumps({'report':str(report),'median':median}),flush=True)
    finally:
        # Only the unique fixture created by this run; retain measurement logs/proof.
        if fixture.is_file():fixture.unlink()

if __name__=='__main__':main()

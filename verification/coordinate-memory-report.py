#!/usr/bin/env python3
"""Before/after same-coordinate fresh JVM diagnostics. No timing or RSS pass thresholds."""
import argparse,datetime,hashlib,json,os,platform,re,statistics,subprocess
from pathlib import Path
from importlib.util import spec_from_file_location, module_from_spec
ROOT=Path(__file__).resolve().parent.parent
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['before','after']);p.add_argument('--target',default='macos-arm64' if platform.system()=='Darwin' else 'linux-amd64');p.add_argument('--skip-compile',action='store_true');a=p.parse_args()
    system=platform.system();assert system in ['Darwin','Linux'],'Real OS RSS diagnostics require macOS or Linux'
    spec=spec_from_file_location('rss_units',ROOT/'verification/window-memory-report.py');rss_units=module_from_spec(spec);spec.loader.exec_module(rss_units)
    if not a.skip_compile:subprocess.run([str(ROOT/'gradlew'),'--console=plain',':gdal4k-runtime:writeWindowMemoryClasspath','-I',str(ROOT/'verification/window-memory.init.gradle'),'--no-configuration-cache'],cwd=ROOT,check=True)
    cp=json.loads((ROOT/'gdal4k-runtime/build/window-memory-classpath.json').read_text())
    library_name='libgdal4k.dylib' if system=='Darwin' else 'libgdal4k.so'
    base=ROOT/'verification/build/coordinate-baseline';manifest=base/'manifest.json' if a.phase=='before' else ROOT/f'gdal4k-engine/build/engine/{a.target}/manifest.json';m=json.loads(manifest.read_text())
    lib=base/library_name if a.phase=='before' else ROOT/f'gdal4k-engine/build/jvm-resources/gdal4k/{a.target}'/library_name
    assert sha(lib)==m['artifacts'][f'jvm-resources/gdal4k/{a.target}/{library_name}']
    output=ROOT/'verification/results/coordinate-memory'/datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');output.mkdir(parents=True,exist_ok=False)
    records=[];modes=['before'] if a.phase=='before' else ['json-batch','binary-batch']
    for trial in range(3):
        for dims in ([2,4] if trial%2==0 else [4,2]):
            for mode in (modes if trial%2==0 else list(reversed(modes))):
                name=f'{trial}-{dims}d-{mode}';log=output/(name+'.log');rss=output/(name+'.time.txt')
                env=os.environ.copy();env['LC_ALL']='C'
                cmd=['/usr/bin/time',('-l' if system=='Darwin' else '-v'),'-o',str(rss),cp['java'],'-Xms32m','-Xmx1536m','-XX:+UseG1GC','-cp',cp['classpath'],'dev.gdal4k.CoordinateMemoryProbe',mode,'262144',str(dims),str(lib)]
                r=subprocess.run(cmd,cwd=ROOT,env=env,text=True,capture_output=True,timeout=300);log.write_text(r.stdout+r.stderr);assert r.returncode==0,(name,r.stderr[-1500:])
                matches=re.findall(r'^GDAL4K_COORDINATE_MEMORY (.+)$',r.stdout,re.M);assert len(matches)==1
                d=json.loads(matches[0]);d['trial']=trial;d['peakRssBytes']=rss_units.peak_rss_bytes(rss.read_text(),system);assert d['points']==262144 and d['rounds']==3 and all(d[k]>0 for k in ['elapsedNanos','jvmAllocatedBytes','sampledPeakHeapUsedBytes','peakRssBytes'])
                assert d['binaryCalls']==(3 if mode=='binary-batch' else 0) and d['jsonCalls']==(0 if mode=='binary-batch' else 3) and d['bridgeBytes']>0
                assert d['binaryCapability']==(a.phase=='after')
                d['log']=str(log.relative_to(ROOT));d['logSha256']=sha(log);d['rssLog']=str(rss.relative_to(ROOT));d['rssLogSha256']=sha(rss);records.append(d);print(json.dumps(d),flush=True)
    summary={f'{dims}d-{mode}':{k:statistics.median(r[k] for r in records if r['dimensions']==dims and r['mode']==mode) for k in ['elapsedNanos','jvmAllocatedBytes','sampledPeakHeapUsedBytes','peakRssBytes']} for dims in [2,4] for mode in modes}
    classes=list((ROOT/'gdal4k-runtime/build/classes/kotlin/jvm/test/dev/gdal4k').glob('CoordinateMemoryProbe*.class'));assert classes
    report={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'phase':a.phase,'platform':platform.platform(),'target':a.target,'records':records,'median':summary,'engineFingerprint':m['source_fingerprint'],'engineManifestSha256':sha(manifest),'engineLibrarySha256':sha(lib),'compiledProbeSha256':{str(x.relative_to(ROOT)):sha(x) for x in classes},'compiledApiSha256':sha(ROOT/'gdal4k/build/classes/kotlin/jvm/main/dev/gdal4k/runtime/CoordinateTransformation.class'),'compiledImplementationSha256':{str(x.relative_to(ROOT)):sha(x) for x in [ROOT/'gdal4k-runtime/build/classes/kotlin/jvm/main/dev/gdal4k/internal/BinaryCoordinatesKt.class',ROOT/'gdal4k-runtime/build/classes/kotlin/jvm/main/dev/gdal4k/internal/BundledCoordinateTransformation.class']},'sourceSha256':{str(x.relative_to(ROOT)):sha(x) for x in [ROOT/'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/BinaryCoordinates.kt',ROOT/'gdal4k/native/runtime-coordinates.hpp',ROOT/'gdal4k/native/gdal4k.cpp',ROOT/'verification/window-memory-report.py']},'probeSourceSha256':sha(ROOT/'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/CoordinateMemoryProbe.kt'),'harnessSha256':sha(Path(__file__)),'settings':{'points':262144,'rounds':3,'trials':3,'crs':['EPSG:4326','EPSG:3857'],'axisMapping':'traditional GIS','jvm':['-Xms32m','-Xmx1536m','-XX:+UseG1GC']},'scope':'Phase before uses a preserved old scalar GDAL engine with current Kotlin legacy fallback; phase after compares JSON fallback and binary transfer on the same current batched GDAL engine. All records include identical input construction and analytical WebMercator result verification. Three rounds in each fresh JVM,128point warmup,2ms heap sampler in all modes. Cumulative JVM allocation, sampled heap and whole-process OS RSS are separate; sampler can miss short peaks. No cold IO, Native/web/Android or universal speed claims. Not zero-copy.'}
    q=ROOT/f'verification/results/coordinate-memory-{a.phase}-{a.target}.json';assert not q.exists(),'Preserve historical report; archive explicitly before a new measurement';q.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(summary),flush=True)

if __name__=='__main__': main()

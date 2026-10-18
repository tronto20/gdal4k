#!/usr/bin/env python3
"""Summarize the same-engine JSON/binary A/B benchmark; never use timing as a correctness threshold."""
import datetime
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys
import xml.etree.ElementTree as E
root=Path(__file__).resolve().parent.parent
target=sys.argv[1]
xml=root/'gdal4k-runtime/build/test-results/jvmTest/TEST-dev.gdal4k.RasterTransportBenchmark.xml'
test=E.parse(xml).getroot()
assert all(int(test.get(key,0))==0 for key in ['failures','errors','skipped'])
text='\n'.join(node.text or '' for node in test.iter('system-out'))
trials=[json.loads(m) for m in re.findall(r'GDAL4K_RASTER_TRANSPORT_BENCHMARK (\{[^\n]+\})',text)]
cancel=[json.loads(m) for m in re.findall(r'GDAL4K_RASTER_CANCEL_BENCHMARK (\{[^\n]+\})',text)]
assert len(trials)==12 and len(cancel)==2 and all(v['destinationUnchanged'] for v in cancel) and {v['protocol'] for v in cancel}=={'json','binary'}
assert all(t['roundTrips']==3 and t['samples']==1048576 and t['sampleBytes'] in [1,8] and t['elapsedNanos']>0 for t in trials)
summary={}
for kind in ['Float64','Byte']:
    summary[kind]={}
    for protocol in ['json','binary']:
        selected=[t for t in trials if t['protocol']==protocol and t['kind']==kind]
        assert len(selected)==3 and {t['trial'] for t in selected}=={0,1,2}
        summary[kind][protocol]={k:statistics.median(t[k] for t in selected) for k in ['elapsedNanos','jvmAllocatedBytes','wireBytes']}
engine=root/'gdal4k-engine/build/engine'/target
manifest=json.loads((engine/'manifest.json').read_text())
proof={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'target':target,'result':'passed','trials':trials,'median':summary,'cancellation':cancel,'sourceSha256':hashlib.sha256((root/'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/RasterTransportBenchmark.kt').read_bytes()).hexdigest(),'xmlSha256':hashlib.sha256(xml.read_bytes()).hexdigest(),'engineFingerprint':manifest['source_fingerprint'],'engineArtifacts':manifest['artifacts'],'scope':'Same JVM/JDK/native engine, alternating JSON/binary on identical MEM Float64/Byte 1024x1024 data after warm-up. 3 measured trials of3 write/read roundtrips. Time excludes final verification. Allocation is cumulative all-JVM-thread bytes, not peak RSS/native memory. Wire count includes ASCII metadata and raw bytes, not copies or compression. Cancellation is cooperative; drivers without callbacks may only stop after current native I/O returns. No cross-platform performance promise or timing gate.'}
output=root/'verification/results'/('raster-transport-'+target+'.json');output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(proof,indent=2)+'\n')
print(json.dumps({'median':summary,'cancellation':cancel}))

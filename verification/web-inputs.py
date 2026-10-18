#!/usr/bin/env python3
"""Verify published web assets, bounded Range reads and permission-selected Files."""
import argparse
import base64
import io
import http.server
import json
import struct
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit, parse_qs
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--repo',type=Path,help='Extract only the published KLIB assets, never use producer files')
p.add_argument('--target',choices=['js','wasmJs'],default='js')
p.add_argument('--http-policy', action='store_true', help='Exercise real GDAL HTTP retry/deadline/cancellation with published assets')
p.add_argument('--browser-only',action='store_true')
p.add_argument('--memory-only',action='store_true',help='Measure external-asset engine initialization instead of input tests')
p.add_argument('--workload',action='store_true',help='Include full-raster checksum and 1024x1024 JXL work in memory measurements')
p.add_argument('--window-only',action='store_true',help='Measure just one 512x512 read instead of the full-raster/JXL workload')
p.add_argument('--profile-label',default='optimized',help='Name the local memory experiment without overwriting its baseline')
a=p.parse_args()
with tempfile.TemporaryDirectory(prefix='gdal4k-web-inputs-') as temporary:
    work=Path(temporary)
    assets=ROOT/'gdal4k-engine/build/generated/web/assets'
    if a.repo:
        suffix='js' if a.target=='js' else 'wasm-js'
        klibs=list((a.repo.resolve()/f'dev/tronto/gdal4k/gdal4k-engine-{suffix}').glob('*/*.klib'))
        if len(klibs)!=1:raise RuntimeError('Expected one published web KLIB')
        assets=work/'assets';assets.mkdir()
        with zipfile.ZipFile(klibs[0]) as z:
            expected={'index.mjs','gdal4k.mjs','gdal4k.wasm','gdal4k-worker.mjs','gdal4k-inputs.mjs','package.json'}
            if not a.memory_only or 'gdal4k-resources.mjs' in z.namelist():expected|={'gdal4k-resources.mjs','gdal4k-data.wasm'}
            if not a.memory_only or 'gdal4k-outputs.mjs' in z.namelist():expected.add('gdal4k-outputs.mjs')
            if not expected.issubset(z.namelist()):raise RuntimeError('Published KLIB is missing its engine assets')
            # Extract the complete published module set: Worker imports may grow as
            # runtime features are added. Never copy producer assets into this test.
            for name in z.namelist():
                if '/' not in name and (name.endswith(('.mjs','.wasm')) or name=='package.json'):
                    (assets/name).write_bytes(z.read(name))
            if z.getinfo('default/ir/strings.knt').file_size > 1024*1024:raise RuntimeError('Web engine is still embedded in Kotlin strings')
    # A sparse, tiled, uncompressed 16384x16384 Byte TIFF: 256 MiB logical size,
    # but only its metadata and first/last tile occupy disk blocks.
    image=work/'large.tif';tiles=4096;tile_bytes=65536;data_offset=65536
    tags=[(256,4,1,16384),(257,4,1,16384),(258,3,1,8),(259,3,1,1),(262,3,1,1),(277,3,1,1),(284,3,1,1),(322,4,1,256),(323,4,1,256),(324,4,tiles,256),(325,4,tiles,256+4*tiles),(33550,12,3,45056),(33922,12,6,45120)]
    with image.open('wb') as f:
        f.write(struct.pack('<2sHI',b'II',42,8));f.write(struct.pack('<H',len(tags)))
        for tag,typ,count,value in tags:f.write(struct.pack('<HHII',tag,typ,count,value))
        f.write(struct.pack('<I',0));f.seek(256)
        f.write(struct.pack('<'+'I'*tiles,*[data_offset+i*tile_bytes for i in range(tiles)]))
        f.write(struct.pack('<'+'I'*tiles,*([tile_bytes]*tiles)))
        f.seek(45056);f.write(struct.pack('<3d',1.0,1.0,0.0))
        f.seek(45120);f.write(struct.pack('<6d',0.0,0.0,0.0,0.0,16384.0,0.0))
        f.seek(data_offset);f.write(bytes([42])*tile_bytes)
        f.seek(data_offset+(tiles-1)*tile_bytes);f.write(bytes([255])*tile_bytes)
    records=[];lock=threading.Lock();policy_counts={};policy_modes={}
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_OPTIONS(self):
            self.send_response(204);self.cors();self.end_headers()
        def cors(self, expose=True):
            self.send_header('Access-Control-Allow-Origin','*')
            self.send_header('Access-Control-Allow-Headers','Range, If-Range')
            if expose:self.send_header('Access-Control-Expose-Headers','Content-Range, ETag, Last-Modified, Content-Length, Retry-After')
        def do_GET(self):
            route=self.path.split('?')[0]
            if route.startswith('/assets/'):
                name=route.removeprefix('/assets/')
                if name not in {f.name for f in assets.iterdir()}:self.send_error(404);return
                file=assets/name;start=0;end=file.stat().st_size-1
                header=self.headers.get('Range','')
                if header:
                    if not header.startswith('bytes='):self.send_error(400);return
                    start,end=map(int,header[6:].split('-'))
                    if not 0<=start<=end<file.stat().st_size:self.send_error(416);return
                self.send_response(206 if header else 200);self.cors()
                if header:self.send_header('Content-Range',f'bytes {start}-{end}/{file.stat().st_size}')
                self.send_header('ETag','"assets-v1"')
                self.send_header('Content-Type','application/wasm' if name.endswith('.wasm') else 'text/javascript')
                self.send_header('Content-Length',str(end-start+1));self.end_headers()
                with file.open('rb') as src:
                    src.seek(start);remaining=end-start+1
                    while remaining:
                        chunk=src.read(min(65536,remaining));self.wfile.write(chunk);remaining-=len(chunk)
                if name=='gdal4k-data.wasm':
                    with lock:records.append({'route':route,'range':header,'status':206 if header else 200,'bytes_sent':end-start+1})
                return
            if route=='/':
                data=b'<!doctype html><input type="file" id="file">'
                self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            if a.http_policy and route=='/control':
                values=parse_qs(urlsplit(self.path).query);key=values['key'][0];mode=values['mode'][0]
                with lock:policy_modes[key]=mode
                self.send_response(204);self.cors();self.end_headers();return
            if a.http_policy and route.startswith('/policy/') and route.endswith('.tif'):
                case=route.rsplit('/',1)[-1][:-4]
                if case not in {'retry','body-timeout','permanent','full','stall-open','backoff','read-cancel','changed','healthy'}:
                    self.send_response(404);self.cors();self.end_headers();return
                header=self.headers.get('Range','')
                if not header.startswith('bytes='):self.send_error(400);return
                start,end=map(int,header[6:].split('-'));total=image.stat().st_size
                with lock:
                    count=policy_counts.get(route,0)+1;policy_counts[route]=count;mode=policy_modes.get(route,'healthy')
                status=401 if case=='permanent' else 200 if case=='full' else 503 if case=='backoff' or case=='retry' and count==1 else 206
                with lock:records.append({'route':route,'range':header,'status':status,'requested_bytes':end-start+1,'if_range':self.headers.get('If-Range'),'mode':mode})
                try:
                    if case=='stall-open' or mode=='stall':time.sleep(1.0)
                    self.send_response(status);self.cors();self.send_header('ETag','"changed"' if mode=='changed' else '"policy-v1"')
                    if status==503:self.send_header('Retry-After','1' if case=='backoff' else '0')
                    if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{total}')
                    self.send_header('Content-Length',str(end-start+1) if status==206 else '0');self.end_headers()
                    if case=='body-timeout' and count==1:time.sleep(0.3)
                    if status==206:
                        with image.open('rb') as src:src.seek(start);self.wfile.write(src.read(end-start+1))
                except (BrokenPipeError,ConnectionResetError):pass
                return
            if route=='/missing.tif':self.send_error(404);return
            # GDAL now probes real sibling names. Unknown sidecars must not return
            # the large TIFF as if it were an XML, DBF, or overview file.
            if route not in {'/large.tif','/no-range.tif','/changed.tif','/wrong-range.tif','/no-cors.tif','/no-expose.tif'}:
                self.send_response(404);self.cors();self.end_headers();return
            header=self.headers.get('Range','')
            if not header.startswith('bytes='):self.send_error(400);return
            start,end=map(int,header[6:].split('-'));total=image.stat().st_size
            if not 0<=start<=end<total:self.send_error(416);return
            status=200 if route=='/no-range.tif' or (route=='/changed.tif' and end>0) else 206
            self.send_response(status)
            if route!='/no-cors.tif':self.cors(expose=route!='/no-expose.tif')
            self.send_header('ETag','"fixture-v1"');self.send_header('Content-Type','image/tiff')
            if status==206:self.send_header('Content-Range',f'bytes {start+1 if route=="/wrong-range.tif" else start}-{end}/{total}')
            self.send_header('Content-Length',str(end-start+1 if status==206 else total));self.end_headers()
            sent=0
            try:
                with image.open('rb') as src:
                    src.seek(start if status==206 else 0);remaining=end-start+1 if status==206 else total
                    while remaining:
                        chunk=src.read(min(65536,remaining));self.wfile.write(chunk);sent+=len(chunk);remaining-=len(chunk)
            except (BrokenPipeError,ConnectionResetError):pass
            finally:
                with lock:records.append({'route':route,'range':header,'status':status,'bytes_sent':sent})
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as z:z.writestr('small.tif',(ROOT/'verification/fixtures/sample.tif').read_bytes())
    config={'binaryOutput':str(work/'binary.json'),'output':str(work/'memory.json'),'zip':base64.b64encode(archive.getvalue()).decode(),'assets':str(assets),'url':url,'file':str(image),'browserOnly':a.browser_only,'root':str(ROOT),'workload':a.workload or a.window_only,'windowOnly':a.window_only,'compactRead':(assets/'gdal4k-resources.mjs').exists()}
    config_file=work/'config.json';config_file.write_text(json.dumps(config))
    try:subprocess.run(['node',str(ROOT/('verification/web-memory.mjs' if a.memory_only else 'verification/http-inputs.mjs' if a.http_policy else 'verification/web-inputs.mjs')),str(config_file)],check=True,cwd=ROOT)
    finally:server.shutdown();server.server_close()
    if a.memory_only:
        if not a.profile_label.replace('-','').isalnum():raise RuntimeError('Invalid profile label')
        destination=ROOT/'verification/results'/('web-engine-memory-'+a.profile_label)
        destination.mkdir(parents=True,exist_ok=True)
        import hashlib
        profile=json.loads((work/'memory.json').read_text())
        profile['assetSha256']={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in assets.iterdir() if f.is_file()}
        profile['resourceReads']=[r for r in records if r['route'].endswith('gdal4k-data.wasm')]
        (destination/'profile.json').write_text(json.dumps(profile,indent=2)+'\n')
        print((destination/'profile.json').read_text())
        raise SystemExit(0)
    if a.http_policy:
        valid=[record for record in records if record['route'].startswith('/policy/')]
        if not valid or any(record['requested_bytes']>65536 for record in valid):raise RuntimeError('Unbounded HTTP policy read')
        for host in ['node','browser']:
            selected=lambda case:[record for record in valid if record['route']==f'/policy/{host}/{case}.tif']
            if len(selected('permanent'))!=1 or len(selected('full'))!=1 or len(selected('backoff'))!=1:raise RuntimeError('Permanent error/backoff retried unexpectedly')
            if sum(record['status']==503 for record in selected('retry'))!=1:raise RuntimeError('Transient retry was not exercised')
            if not any(record['if_range']=='"policy-v1"' for record in selected('healthy')):raise RuntimeError('Conditional identity reads not exercised')
        destination=ROOT/'verification/results'/('http-inputs-'+a.target);destination.mkdir(parents=True,exist_ok=True)
        (destination/'result.json').write_text(json.dumps({'target':a.target,'published_assets':bool(a.repo),'binary_raster':True,'logical_file_bytes':image.stat().st_size,'requests':records,'result':'passed'},indent=2)+'\n')
        print('GDAL4K_HTTP_INPUT_POLICY_OK',a.target,'Node+Chromium',flush=True)
        raise SystemExit(0)
    binary=json.loads((work/'binary.json').read_text())
    if set(binary)!=({'Chromium'} if a.browser_only else {'Node','Chromium'}) or any(v['result']!='passed' for v in binary.values()):raise RuntimeError('Binary protocol verification incomplete')
    destination=ROOT/'verification/results'/('binary-transport-'+a.target);destination.mkdir(parents=True,exist_ok=True)
    (destination/'result.json').write_text(json.dumps({'result':'passed','target':a.target,'published_assets':bool(a.repo),'platforms':binary},indent=2)+'\n')
    valid=[r for r in records if r['route']=='/large.tif']
    if not valid or any(r['status']!=206 or r['bytes_sent']>65536 for r in valid):raise RuntimeError('Unbounded HTTP read')
    total_read=sum(r['bytes_sent'] for r in valid)
    if total_read>2*1024*1024:raise RuntimeError('Small windows fetched too much of the large TIFF')
    resources=[r for r in records if r['route'].endswith('gdal4k-data.wasm')]
    if not resources or any(r['status']!=206 or not r['range'] for r in resources):raise RuntimeError('GDAL resources were not read by range')
    if sum(r['bytes_sent'] for r in resources) >= (assets/'gdal4k-data.wasm').stat().st_size:raise RuntimeError('Unused GDAL resources were downloaded')
    destination=ROOT/'verification/results'/('web-inputs-'+a.target);destination.mkdir(parents=True,exist_ok=True)
    (destination/'result.json').write_text(json.dumps({'target':a.target,'published_assets':bool(a.repo),'logical_file_bytes':image.stat().st_size,'http_bytes_sent':total_read,'resource_asset_bytes':(assets/'gdal4k-data.wasm').stat().st_size,'resource_bytes_sent':sum(r['bytes_sent'] for r in resources),'requests':records,'result':'passed'},indent=2)+'\n')
    print('GDAL4K_WEB_DIRECT_INPUTS_OK',total_read,'bytes fetched from',image.stat().st_size,flush=True)

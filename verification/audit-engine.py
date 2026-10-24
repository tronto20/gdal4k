#!/usr/bin/env python3
"""Fail packaging if a runtime needs producer-installed GDAL or codec libraries."""
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from windows_pe import validate_system_dependencies

ROOT=Path(__file__).resolve().parent.parent
p=argparse.ArgumentParser();p.add_argument('platform');a=p.parse_args()
platform=a.platform
base=ROOT/'gdal4k-engine/build'
dependencies=[]
if platform=='web':
    wasm=base/'engine/web/gdal4k.wasm'
    if not wasm.is_file() or wasm.read_bytes()[:4] != b'\0asm':raise RuntimeError('Missing separate WebAssembly asset')
    opt=Path(os.environ['EMSDK'])/'upstream/bin'/('wasm-opt.exe' if os.name=='nt' else 'wasm-opt')
    with tempfile.TemporaryDirectory(prefix='gdal4k-wasm-audit-') as temp:
        subprocess.run([str(opt),str(wasm),'--enable-bulk-memory','--enable-sign-ext','--enable-mutable-globals','--enable-nontrapping-float-to-int','--disable-gc','--disable-threads','--disable-simd','--disable-exception-handling','--print-features','-o',str(Path(temp)/'validated.wasm')],check=True)
        assets=base/'generated/web/assets'
        data=assets/'gdal4k-data.wasm'
        subprocess.run([str(opt),str(data),'--mvp-features','-o',str(Path(temp)/'validated-data.wasm')],check=True)
        entries=json.loads(re.search(r'export const resources = (.*);', (assets/'gdal4k-resources.mjs').read_text())[1])
        prefix=base/'engine/web/installed/gdal4k-web/share/gdal'
        sys_path=ROOT/'gdal4k-engine/tools'
        import sys
        sys.path.insert(0,str(sys_path))
        from gdal_resources import resource_files
        expected={p.relative_to(prefix).as_posix():p.read_bytes() for p in resource_files(prefix.parent.parent)}
        if {entry['name'] for entry in entries}!=set(expected):raise RuntimeError('Incomplete web resource asset')
        payload=data.read_bytes()
        for entry in entries:
            if payload[entry['offset']:entry['offset']+entry['size']]!=expected[entry['name']]:raise RuntimeError('Corrupt bundled resource: '+entry['name'])
    dependencies=['ordinary Wasm; GC/threads/SIMD/EH disabled during validation']
elif platform.startswith(('macos','ios')):
    library=base/f'engine/{platform}/cmake/libgdal4k.dylib'
    dependencies=[line.strip().split(' (')[0] for line in subprocess.check_output(['otool','-L',str(library)],text=True).splitlines()[1:]]
    unexpected=[d for d in dependencies if not d.startswith(('/usr/lib/','/System/Library/','@rpath/libgdal4k.dylib'))]
    if unexpected:raise SystemExit('Unbundled Apple dependencies: '+str(unexpected))
else:
    library=base/(f'android-runtime/jniLibs/{platform[8:]}/libgdal4k.so' if platform.startswith('android') else f'jvm-resources/gdal4k/{platform}/'+('gdal4k.dll' if platform.startswith('windows') else 'libgdal4k.so'))
    if platform.startswith('android'):
        ndk=Path(os.environ.get('ANDROID_NDK_HOME',ROOT/'verification/tools/android-ndk-r28c'))
        objdump=next((ndk/'toolchains/llvm/prebuilt').glob('*/bin/llvm-objdump'+('.exe' if os.name=='nt' else '')))
    elif platform.startswith('windows'):
        objdump=Path(os.environ.get('KONAN_DATA_DIR',Path.home()/'.konan'))/'dependencies/msys2-mingw-w64-x86_64-2/bin/objdump.exe'
    else:
        directory=Path(os.environ.get('KONAN_DATA_DIR',Path.home()/'.konan'))/'dependencies'
        objdump=next(directory.glob('llvm-21-*/bin/llvm-objdump'),None) or shutil.which('llvm-objdump') or shutil.which('objdump')
        if not objdump:raise SystemExit('An ELF-capable objdump is required for the producer dependency audit')
    output=subprocess.check_output([str(objdump),'-p',str(library)],text=True)
    if platform.startswith('windows'):
        dependencies=re.findall(r'DLL Name:\s*(\S+)',output)
        try:validate_system_dependencies(dependencies)
        except ValueError as failure:raise SystemExit(str(failure))
        unexpected=[]
    else:
        dependencies=re.findall(r'NEEDED\s+(\S+)',output)
        allowed={'libc.so','libm.so','libdl.so','liblog.so','libz.so','libandroid.so'} if platform.startswith('android') else {'libc.so.6','libm.so.6','libdl.so.2','libpthread.so.0','librt.so.1','ld-linux-x86-64.so.2','ld-linux-aarch64.so.1'}
        unexpected=[d for d in dependencies if d not in allowed]
    if not dependencies:raise SystemExit('Could not inspect runtime dependencies')
    if unexpected:raise SystemExit('Unbundled dependencies: '+str(unexpected))
result={'platform':platform,'dependencies':dependencies,'result':'passed'}
destination=ROOT/f'verification/results/engine-audit-{platform}.json';destination.parent.mkdir(parents=True,exist_ok=True)
destination.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))

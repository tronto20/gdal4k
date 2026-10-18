#!/usr/bin/env python3
"""Build, execute and stage the actual library artifacts, never a system GDAL probe."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from jvm_binaries import JVM_BINARIES, host_binary
from publication_versions import ANDROID_ENGINE, ENGINE_COORDINATES, ENGINE_TARGETS

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT/'gdal4k-engine/build'
REPO = ROOT/'verification/build/consumer-maven'
GRADLE = ROOT/('gradlew.bat' if os.name == 'nt' else 'gradlew')
PLATFORMS = {'macosArm64':'macos-arm64','linuxX64':'linux-amd64',
 'linuxArm64':'linux-arm64','mingwX64':'windows-amd64','iosArm64':'ios-arm64',
 'iosSimulatorArm64':'ios-simulator-arm64'}

def run(*args):
    subprocess.run([str(x) for x in args], cwd=ROOT, check=True)

def gradle(*tasks, options=()):
    run(GRADLE, '--console=plain', f'-Dmaven.repo.local={REPO}', *options, *tasks)

def publish(target):
    title=target[0].upper()+target[1:]
    modules=['gdal4k','gdal4k-runtime']
    if target.lower().replace('wasmjs','wasm-js') in ENGINE_TARGETS:modules+=['gdal4k-engine']
    gradle(*(f':{m}:publish{p}PublicationToMavenLocal' for m in modules for p in ['KotlinMultiplatform',title]))

def consumer(target, *options):
    run(sys.executable, ROOT/'verification/consumer.py', '--target', target, '--no-publish', '--repo', REPO, *options)

def stage(name, platforms=(), targets=(), include_root=False, include_tests=False):
    """Only portable outputs; no producer installations, SDKs or build caches."""
    files=set()
    for platform in platforms:
        manifest=BUILD/f'engine/{platform}/manifest.json'
        data=json.loads(manifest.read_text())
        files.add(manifest)
        files.update(BUILD/f for f in data['artifacts'])
    suffixes={'wasmJs':'wasm-js'}
    coordinates=[m+'-'+suffixes.get(t,t.lower()) for m in ['gdal4k-runtime','gdal4k'] for t in targets]
    coordinates+=[ENGINE_COORDINATES[suffixes.get(t,t.lower())] for t in targets if suffixes.get(t,t.lower()) in ENGINE_TARGETS]
    if include_root:coordinates+=['gdal4k-runtime','gdal4k','gdal4k-engine',ANDROID_ENGINE]
    for coordinate in coordinates:
        directory=REPO/'dev/tronto/gdal4k'/coordinate
        if not directory.exists():raise RuntimeError('Missing publication '+coordinate)
        files.update(f for f in directory.rglob('*') if f.is_file())
    if include_tests:
        files.update(f for f in (ROOT/'gdal4k-runtime/build/bin').rglob('*') if f.is_file() and 'debugTest' in f.parts)
        for result in (ROOT/'verification/results').glob('published-consumer-linuxArm64/bin/**/*'):
            if result.is_file():files.add(result)
    output=ROOT/f'verification/build/staged/{name}.tar.gz';output.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(output,'w:gz') as archive:
        for f in sorted(files):archive.add(f,arcname=f.relative_to(ROOT),recursive=False)
    print('Staged '+str(output),flush=True)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('target', choices=[*PLATFORMS,'web','android-arm64-v8a','aggregate','linuxArm64-run'])
    p.add_argument('--build-engine-only',action='store_true',help='Build the native engine before starting a simulator or running Kotlin tests')
    p.add_argument('--ios-device');a=p.parse_args();target=a.target
    if a.build_engine_only and target not in PLATFORMS:p.error('--build-engine-only requires a Native target')
    if target in PLATFORMS:
        gradle(f':gdal4k:compileKotlin{target[0].upper()+target[1:]}') # initialize matching K/N toolchain
        if a.build_engine_only:
            gradle(':gdal4k-engine:buildEngine'+''.join(part.title() for part in PLATFORMS[target].split('-')))
            return
        cross=target in ['iosArm64','linuxArm64']
        options=[f'-PiosDevice={a.ios_device}'] if a.ios_device else []
        tasks=[f':gdal4k-runtime:linkDebugTest{target[0].upper()+target[1:]}'] if cross else [f':gdal4k-runtime:{target}Test']
        if target in ['macosArm64','linuxX64','mingwX64']:tasks+=[':gdal4k:jvmTest',':gdal4k-runtime:jvmTest']
        gradle(*tasks,options=options)
        if target in ['macosArm64','linuxX64','mingwX64']:run(sys.executable,ROOT/'verification/raster-transport-report.py',PLATFORMS[target])
        if target in ['macosArm64','linuxX64']:run(sys.executable,ROOT/'verification/window-memory-report.py','--target',PLATFORMS[target])
        if target in ['macosArm64','linuxX64']:run(sys.executable,ROOT/'verification/coordinate-memory-report.py','after','--target',PLATFORMS[target],'--skip-compile')
        if target in ['macosArm64','linuxX64']:
            library='libgdal4k.dylib' if target=='macosArm64' else 'libgdal4k.so'
            run(sys.executable, ROOT/'verification/vector-memory-report.py',
                '--library', BUILD/f'jvm-resources/gdal4k/{PLATFORMS[target]}/{library}',
                '--manifest', BUILD/f'engine/{PLATFORMS[target]}/manifest.json',
                '--label', 'ci-current', '--modes', 'features', 'columns')
        run(sys.executable, ROOT/'verification/audit-engine.py', PLATFORMS[target])
        publish(target)
        consumer(target,*(['--compile-only'] if cross else []),*(['--ios-device',a.ios_device] if a.ios_device else []))
        stage(target,[PLATFORMS[target]],[target],include_tests=target=='linuxArm64')
    elif target=='web':
        run('node', ROOT/'verification/web-output-modes.mjs')
        gradle(':gdal4k-runtime:jsNodeTest',':gdal4k-runtime:wasmJsNodeTest',':gdal4k-runtime:jsBrowserTest',':gdal4k-runtime:wasmJsBrowserTest')
        run('node', '--test', ROOT/'verification/http-input-policy.test.mjs')
        run('node', ROOT/'verification/js-codec-benchmark.cjs', 'after')
        run(sys.executable, ROOT/'verification/audit-engine.py', 'web')
        for t in ['js','wasmJs']:
            publish(t);consumer(t);consumer(t,'--browser')
            run(sys.executable, ROOT/'verification/web-inputs.py', '--repo', REPO, '--target', t)
            run(sys.executable, ROOT/'verification/web-inputs.py', '--repo', REPO, '--target', t, '--http-policy')
            run(sys.executable, ROOT/'verification/related-inputs.py', '--repo', REPO, '--target', t)
        stage('web',['web'],['js','wasmJs'])
    elif target.startswith('android-'):
        gradle(':gdal4k-engine:buildEngine'+''.join(part.title() for part in target.split('-')))
        run(sys.executable,ROOT/'verification/audit-engine.py',target)
        stage(target,[target])
    elif target=='aggregate':
        # All four JVM JNI libraries and Android ARM64 are mandatory before publication.
        engines=[*PLATFORMS.values(),'web','android-arm64-v8a']
        fingerprints=set()
        for platform in engines:
            manifest=json.loads((BUILD/f'engine/{platform}/manifest.json').read_text())
            if manifest['target']!=platform or manifest['gdal']!='3.12.4' or manifest['jxl'] is not True:
                raise RuntimeError('Unexpected staged engine profile: '+platform)
            fingerprints.add(manifest['source_fingerprint'])
            for name,digest in manifest['artifacts'].items():
                if hashlib.sha256((BUILD/name).read_bytes()).hexdigest()!=digest:
                    raise RuntimeError('Staged engine payload does not match its manifest: '+name)
        if len(fingerprints)!=1:raise RuntimeError('Staged engines were built from different sources')
        gradle(':gdal4k-runtime:jvmTest')
        publish('jvm')
        gradle(':gdal4k-engine:publishKotlinMultiplatformPublicationToMavenLocal',options=['-Pgdal4kJvmClassifiers='+','.join(JVM_BINARIES)])
        run(sys.executable, ROOT/'verification/check-jvm-publications.py', '--repo', REPO)
        import zipfile
        publish('android')
        gradle(':gdal4k-android-runtime:publishAndroidRuntimePublicationToMavenLocal')
        aar=next((REPO/'dev/tronto/gdal4k'/ANDROID_ENGINE).glob('*/*.aar'))
        with zipfile.ZipFile(aar) as z:
            expected={'jni/arm64-v8a/libgdal4k.so'}
            actual={name for name in z.namelist() if name.startswith('jni/') and name.endswith('.so')}
            if actual!=expected:raise RuntimeError(f'Unexpected Android AAR libraries: {sorted(actual)}; expected {sorted(expected)}')
        consumer('jvm')
        consumer('jvm', '--expect-missing-jvm-binary')
        wrong_binary = next(b for b in JVM_BINARIES if b != host_binary())
        consumer('jvm', '--jvm-binary', wrong_binary, '--expect-missing-jvm-binary')
        consumer('jvm', '--jvm-binary', host_binary(), '--jvm-binary', wrong_binary)
        run(sys.executable, ROOT/'verification/check-optional-binaries.py', '--repo', REPO)
        run(sys.executable, ROOT/'verification/check-engine-variants.py', '--repo', REPO)
        run(sys.executable, ROOT/'verification/engine-consumer.py', '--repo', REPO, *[item for b in JVM_BINARIES for item in ['--jvm-binary', b]])
        # Exercise the release publisher against a local repository, including snapshot metadata.
        published=ROOT/'verification/build/publish-roundtrip'
        run(sys.executable, ROOT/'verification/publish.py','--repo',REPO,'--destination',published)
        run(sys.executable, ROOT/'verification/consumer.py','--target','jvm','--no-publish','--repo',published)
        run(sys.executable, ROOT/'verification/engine-consumer.py', '--repo', published, '--targets', 'jvm', '--jvm-binary', host_binary())
        stage('complete-maven',targets=[*PLATFORMS,'js','wasmJs','jvm','android'],include_root=True)
        stage('jvm-maven',targets=['jvm'],include_root=True)
    elif target=='linuxArm64-run':
        # Kotlin/Native has no Linux ARM64 compiler host. Execute the cross-linked tests here.
        native=ROOT/'gdal4k-runtime/build/bin/linuxArm64/debugTest/test.kexe'
        run(native)
        binaries=list((ROOT/'verification/results/published-consumer-linuxArm64/bin').glob('**/test.kexe'))
        if len(binaries)!=1:raise RuntimeError('Missing independent consumer executable')
        run(binaries[0])
        consumer('jvm')

if __name__=='__main__':main()

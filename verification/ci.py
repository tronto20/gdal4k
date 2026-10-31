#!/usr/bin/env python3
"""Build, execute and stage the actual library artifacts, never a system GDAL probe."""
import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from jvm_binaries import JVM_BINARIES, host_binary
from publication_versions import ANDROID_ENGINE, ENGINE_COORDINATES, ENGINE_TARGETS
from command_evidence import run_with_evidence
from archive_transport import write_archive

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT/'gdal4k-engine/build'
REPO = ROOT/'verification/build/consumer-maven'
GRADLE = ROOT/('gradlew.bat' if os.name == 'nt' else 'gradlew')
PLATFORMS = {'macosArm64':'macos-arm64','linuxX64':'linux-amd64',
 'linuxArm64':'linux-arm64','mingwX64':'windows-amd64','iosArm64':'ios-arm64',
 'iosSimulatorArm64':'ios-simulator-arm64'}
COMMAND_TIMEOUT = None
COMMAND_EVIDENCE = None
WINDOWS_COLUMN_DIAGNOSTICS = False
WINDOWS_NATIVE_DIAGNOSTICS = False
NATIVE_DIAGNOSTIC_BUDGET = None

def run(*args, env=None, windows_column_diagnostics=False, windows_column_dll_sha256=None):
    if COMMAND_TIMEOUT is not None:
        run_with_evidence(args, cwd=ROOT, directory=COMMAND_EVIDENCE, timeout=COMMAND_TIMEOUT, env=env,
                          windows_column_diagnostics=windows_column_diagnostics,
                          windows_column_dll_sha256=windows_column_dll_sha256)
    else:
        subprocess.run([str(x) for x in args], cwd=ROOT, check=True, env=env)

def gradle(*tasks, options=()):
    diagnostic_options = ['--no-daemon', '--init-script', ROOT/'verification/ci-diagnostics.init.gradle'] if COMMAND_TIMEOUT is not None else []
    column_diagnostics = WINDOWS_COLUMN_DIAGNOSTICS and any(task.endswith(':jvmTest') for task in tasks)
    environment, dll_sha = None, None
    if column_diagnostics:
        from jvm_hang_diagnostics import prepare_jvm_hang_diagnostics
        agent = prepare_jvm_hang_diagnostics(ROOT/'gdal4k-runtime')
        environment = os.environ.copy()
        environment['GDAL4K_SOURCE_JVM_AGENT64'] = base64.urlsafe_b64encode(agent['jvm_arg'].encode('utf-8')).decode('ascii')
        manifest = json.loads((BUILD/'engine/windows-amd64/manifest.json').read_text())
        dll_sha = manifest['artifacts']['jvm-resources/gdal4k/windows-amd64/gdal4k.dll']
    run(GRADLE, '--console=plain', *diagnostic_options, f'-Dmaven.repo.local={REPO}', *options, *tasks,
        env=environment, windows_column_diagnostics=column_diagnostics, windows_column_dll_sha256=dll_sha)

def publish(target):
    title=target[0].upper()+target[1:]
    modules=['gdal4k','gdal4k-runtime']
    if target.lower().replace('wasmjs','wasm-js') in ENGINE_TARGETS:modules+=['gdal4k-engine']
    gradle(*(f':{m}:publish{p}PublicationToMavenLocal' for m in modules for p in ['KotlinMultiplatform',title]))

def consumer(target, *options):
    # Let the consumer's finally blocks retain XML and restore hidden producer
    # paths before the outer command deadline terminates its Python process.
    diagnostic_options = ['--command-timeout-seconds', max(1, COMMAND_TIMEOUT - 60)] if COMMAND_TIMEOUT is not None else []
    if COMMAND_TIMEOUT is not None:
        reserve = min(15, COMMAND_TIMEOUT / 4)
        diagnostic_options += ['--completion-deadline-monotonic', time.monotonic() + COMMAND_TIMEOUT - reserve]
    if WINDOWS_NATIVE_DIAGNOSTICS and target == 'mingwX64':
        diagnostic_options += ['--windows-native-consumer-diagnostics', '--native-diagnostic-budget-bytes', NATIVE_DIAGNOSTIC_BUDGET]
    run(sys.executable, ROOT/'verification/consumer.py', '--target', target, '--no-publish', '--repo', REPO, *diagnostic_options, *options)

def stage(name, platforms=(), targets=(), include_root=False, include_tests=False, archive_format='gzip'):
    """Only portable outputs; no producer installations, SDKs or build caches."""
    files=set()
    for platform in platforms:
        manifest=BUILD/f'engine/{platform}/manifest.json'
        data=json.loads(manifest.read_text())
        files.add(manifest)
        for metadata_name in ('sdk-toolchain-inputs.json', 'sdk-source-inputs.json'):
            metadata = manifest.parent / metadata_name
            if metadata.is_symlink() or not metadata.is_file():
                raise RuntimeError('Missing regular SDK input metadata: ' + str(metadata))
            files.add(metadata)
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
    suffix = {'gzip': 'gz', 'xz': 'xz'}[archive_format]
    output=ROOT/f'verification/build/staged/{name}.tar.{suffix}'
    summary = write_archive(output, sorted(files), ROOT)
    print('Staged '+str(output),flush=True)
    print(json.dumps({'stagedArchive': summary}, sort_keys=True), flush=True)

def main():
    global COMMAND_TIMEOUT, COMMAND_EVIDENCE, WINDOWS_COLUMN_DIAGNOSTICS
    global WINDOWS_NATIVE_DIAGNOSTICS, NATIVE_DIAGNOSTIC_BUDGET
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('target', choices=[*PLATFORMS,'web','android-arm64-v8a','aggregate','linuxArm64-run'])
    p.add_argument('--build-engine-only',action='store_true',help='Build the native engine before starting a simulator or running Kotlin tests')
    p.add_argument('--command-timeout-seconds', type=int,
                   help='Bound each child command and retain output under verification/results/ci-commands-TARGET')
    p.add_argument('--windows-column-diagnostics', action='store_true',
                   help='Capture owned native/Java stacks and bound stalled source JVM column reads')
    p.add_argument('--windows-native-consumer-diagnostics', action='store_true',
                   help='Retain pre-run Native consumer identity and bounded owned-process stacks')
    p.add_argument('--native-diagnostic-budget-bytes', type=int,
                   help='Explicit total Native diagnostic ceiling, at most512MiB')
    p.add_argument('--archive-format', choices=['gzip', 'xz'], default='gzip',
                   help='Compression of staged CI transport archives (legacy default: gzip)')
    p.add_argument('--ios-device');a=p.parse_args();target=a.target
    if a.windows_column_diagnostics:
        if os.name != 'nt' or target != 'mingwX64' or a.build_engine_only or a.command_timeout_seconds is None:
            p.error('--windows-column-diagnostics requires a bounded Windows mingwX64 source-test command')
        WINDOWS_COLUMN_DIAGNOSTICS = True
    if a.windows_native_consumer_diagnostics:
        if os.name != 'nt' or target != 'mingwX64' or a.build_engine_only or a.command_timeout_seconds is None:
            p.error('Native consumer diagnostics require a bounded Windows mingwX64 test command')
        from native_consumer_diagnostics import valid_budget
        try: NATIVE_DIAGNOSTIC_BUDGET = valid_budget(a.native_diagnostic_budget_bytes)
        except ValueError as error: p.error(str(error))
        WINDOWS_NATIVE_DIAGNOSTICS = True
    elif a.native_diagnostic_budget_bytes is not None:
        p.error('A Native diagnostic budget requires --windows-native-consumer-diagnostics')
    if a.command_timeout_seconds is not None:
        if a.command_timeout_seconds <= 0:p.error('--command-timeout-seconds must be positive')
        COMMAND_TIMEOUT = a.command_timeout_seconds
        COMMAND_EVIDENCE = ROOT / f'verification/results/ci-commands-{target}'
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
            run(sys.executable, ROOT/'verification/file-memory-report.py',
                '--library', BUILD/f'jvm-resources/gdal4k/{PLATFORMS[target]}/{library}',
                '--manifest', BUILD/f'engine/{PLATFORMS[target]}/manifest.json',
                '--label', 'ci-current')
            run(sys.executable, ROOT/'verification/md-memory-report.py',
                '--library', BUILD/f'jvm-resources/gdal4k/{PLATFORMS[target]}/{library}',
                '--manifest', BUILD/f'engine/{PLATFORMS[target]}/manifest.json',
                '--label', 'ci-current')
            run(sys.executable, ROOT/'verification/footprint-memory-report.py',
                '--library', BUILD/f'jvm-resources/gdal4k/{PLATFORMS[target]}/{library}',
                '--manifest', BUILD/f'engine/{PLATFORMS[target]}/manifest.json',
                '--label', 'ci-current')
            run(sys.executable, ROOT/'verification/tile-index-memory-report.py',
                '--library', BUILD/f'jvm-resources/gdal4k/{PLATFORMS[target]}/{library}',
                '--manifest', BUILD/f'engine/{PLATFORMS[target]}/manifest.json',
                '--label', 'ci-current')
            run(sys.executable, ROOT/'verification/md-translate-memory-report.py',
                '--library', BUILD/f'jvm-resources/gdal4k/{PLATFORMS[target]}/{library}',
                '--manifest', BUILD/f'engine/{PLATFORMS[target]}/manifest.json',
                '--label', 'ci-current')
        run(sys.executable, ROOT/'verification/audit-engine.py', PLATFORMS[target])
        publish(target)
        consumer(target,*(['--compile-only'] if cross else []),*(['--ios-device',a.ios_device] if a.ios_device else []))
        stage(target,[PLATFORMS[target]],[target],include_tests=target=='linuxArm64',archive_format=a.archive_format)
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
        stage('web',['web'],['js','wasmJs'],archive_format=a.archive_format)
    elif target.startswith('android-'):
        gradle(':gdal4k-engine:buildEngine'+''.join(part.title() for part in target.split('-')))
        run(sys.executable,ROOT/'verification/audit-engine.py',target)
        stage(target,[target],archive_format=a.archive_format)
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
        stage('complete-maven',targets=[*PLATFORMS,'js','wasmJs','jvm','android'],include_root=True,archive_format=a.archive_format)
        stage('jvm-maven',targets=['jvm'],include_root=True,archive_format=a.archive_format)
    elif target=='linuxArm64-run':
        # Kotlin/Native has no Linux ARM64 compiler host. Execute the cross-linked tests here.
        native=ROOT/'gdal4k-runtime/build/bin/linuxArm64/debugTest/test.kexe'
        run(native)
        binaries=list((ROOT/'verification/results/published-consumer-linuxArm64/bin').glob('**/test.kexe'))
        if len(binaries)!=1:raise RuntimeError('Missing independent consumer executable')
        run(binaries[0])
        consumer('jvm')

if __name__=='__main__':main()

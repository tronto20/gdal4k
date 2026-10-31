#!/usr/bin/env python3
"""Build the GDAL runtime from pinned sources. Consumers never run this script."""
import argparse, hashlib, json, os, platform, re, shlex, shutil, subprocess
from contextlib import contextmanager
from pathlib import Path
from toolchains import native_toolchain
from versions import configured_binary_version
from staged_engine import verify_staged_engine
from sdk_cache_identity import prepare_sdk_cache_inputs
ROOT=Path(__file__).resolve().parents[2]
BINARY=ROOT/'gdal4k-engine'
VCPKG_REV='3cd21e48a75f67acfef289da52893ce79144aaa1'
TARGETS={
 'macos-arm64':'arm64-osx',
 'linux-amd64':'x64-linux','linux-arm64':'arm64-linux',
 'windows-amd64':'x64-mingw-static',
 'ios-arm64':'arm64-ios','ios-simulator-arm64':'arm64-ios-simulator',
 'android-arm64-v8a':'arm64-android',
 'web':'wasm32-emscripten',
}
def run(*args,**kwargs):subprocess.run([str(a) for a in args],check=True,**kwargs)

def link_arguments(command):
 # CMake wraps Windows link + post-build commands in cmd /C "...".
 # Remove that outer shell quoting before reading the linker's archive arguments.
 wrapper=re.match(r'^.*?\bcmd\.exe\s+/C\s+"(.*)"$',command,re.I)
 if wrapper:command=wrapper.group(1)
 return shlex.split(command.replace('\\','/'))

def ninja_link_arguments(ninja,build,output):
 # Long link commands use response files that Ninja removes after linking.
 # compdb -x expands their graph content even when those files no longer exist.
 database=json.loads(subprocess.check_output([str(ninja),'-C',str(build),'-t','compdb','-x'],text=True))
 directory=build.resolve();expected=directory/output
 matches=[entry for entry in database
          if Path(entry['directory']).resolve()==directory
          and (directory/entry['output']).resolve()==expected]
 if len(matches)!=1:raise RuntimeError(f'Expected one expanded Ninja link command for {expected}, found {len(matches)}')
 arguments=link_arguments(matches[0]['command'])
 if any(token.startswith('@') and not token.startswith(('@rpath/','@loader_path/','@executable_path/')) for token in arguments):
  raise RuntimeError('Unexpanded Ninja response file in link command for '+str(expected))
 return arguments
def fingerprint():
 digest=hashlib.sha256()
 digest.update(configured_binary_version().encode())
 for directory in [ROOT/'gdal4k/native',BINARY/'ports',BINARY/'engine',BINARY/'tools',BINARY/'web']:
  # WindowsPath sorts without case; compare the same slash-separated names on every host.
  for source in sorted(directory.rglob('*'),key=lambda p:p.relative_to(ROOT).as_posix()):
   if source.is_file() and '__pycache__' not in source.parts:
    digest.update(source.relative_to(ROOT).as_posix().encode());digest.update(source.read_bytes().replace(b'\r\n',b'\n'))
 return digest.hexdigest()
def reusable(out,source_hash):
 manifest=out/'manifest.json'
 if not manifest.is_file():return False
 data=json.loads(manifest.read_text())
 if data.get('source_fingerprint')!=source_hash:return False
 return all((BINARY/'build'/name).is_file() and hashlib.sha256((BINARY/'build'/name).read_bytes()).hexdigest()==digest for name,digest in data.get('artifacts',{}).items()) and bool(data.get('artifacts'))

def vcpkg_work_options(out):
 # Only immutable tool sources/downloads/binary caches are shared across projects.
 # Both direct installation and CMake's manifest install use this same workspace.
 if not out.resolve().is_relative_to(ROOT.resolve()):
  raise RuntimeError('Refusing vcpkg workspace outside the producer checkout: '+str(out))
 for parent in (out,*out.parents):
  if parent==ROOT:break
  if parent.is_symlink():
   raise RuntimeError('Refusing shared or symlinked vcpkg workspace: '+str(parent))
 for name in ('installed','vcpkg-buildtrees','vcpkg-packages'):
  path=out/name
  if path.is_symlink() or not path.resolve().is_relative_to(out.resolve()):
   raise RuntimeError('Refusing shared or symlinked vcpkg workspace: '+str(path))
 return [f'--x-buildtrees-root={out/"vcpkg-buildtrees"}',f'--x-packages-root={out/"vcpkg-packages"}']

def require_private_install_prefix(out):
 # A cache miss must never turn an explicitly shared/read-only prefix into an installer target.
 if (out/'installed').is_symlink():
  raise RuntimeError('Refusing vcpkg installation through an installed-prefix symlink; use an isolated install prefix or an explicit read-only rebuild')

def main():
 p=argparse.ArgumentParser();p.add_argument('target',choices=TARGETS);p.add_argument('--jobs',type=int,default=4);p.add_argument('--vcpkg',type=Path);p.add_argument('--build-tools',type=Path)
 p.add_argument('--verify-staged-only',action='store_true',help='Verify the current Linux ARM64 engine without a compiler, installer or build fallback')
 a=p.parse_args()
 if a.verify_staged_only:
  if a.vcpkg is not None or a.build_tools is not None:p.error('--verify-staged-only cannot configure producer tools')
  receipt=verify_staged_engine(BINARY,a.target,fingerprint,VCPKG_REV,configured_binary_version())
  print('Verified staged GDAL engine without producer execution: '+a.target+' ('+receipt['sourceFingerprint']+')');return
 source_hash=fingerprint()
 out=BINARY/'build/engine'/a.target;out.mkdir(parents=True,exist_ok=True)
 if reusable(out,source_hash):
  print('Verified prebuilt GDAL engine: '+a.target);return
 require_private_install_prefix(out)
 # Gradle tool preparation writes paths only; it does not alter the parent shell.
 if a.build_tools:
  values=json.loads(a.build_tools.read_text(encoding='utf-8'))
  prefixes=values.pop('PATH_PREFIX','')
  os.environ.update(values)
  if values.get('VCPKG_FORCE_SYSTEM_BINARIES'):
   os.environ.pop('VCPKG_FORCE_DOWNLOADED_BINARIES',None)
  if prefixes:os.environ['PATH']=prefixes+os.pathsep+os.environ.get('PATH','')
 cmake=os.environ.get('CMAKE_COMMAND','cmake')
 ninja=os.environ.get('NINJA_COMMAND','ninja')
 vcpkg=(a.vcpkg or Path(os.environ.get('VCPKG_ROOT',ROOT/'verification/tools/vcpkg'))).resolve()
 # A configurable store may be shared by independent checkouts. vcpkg's own
 # install lock does not cover the following CMake configure/link operations.
 with build_lock(vcpkg/'.gdal4k-build.lock'):
  build(a,vcpkg,out,source_hash,cmake,ninja)

def build(a,vcpkg,out,source_hash,cmake,ninja):
 work_options=vcpkg_work_options(out)
 if not (vcpkg/'.git').exists():
  vcpkg.parent.mkdir(parents=True,exist_ok=True);run('git','init',vcpkg);run('git','-C',vcpkg,'fetch','--depth','1','https://github.com/microsoft/vcpkg.git',VCPKG_REV);run('git','-C',vcpkg,'checkout','--detach','FETCH_HEAD')
 rev=subprocess.check_output(['git','-C',str(vcpkg),'rev-parse','HEAD'],text=True).strip()
 if rev!=VCPKG_REV:raise SystemExit(f'Expected pinned vcpkg {VCPKG_REV}, found {rev}')
 exe=vcpkg/('vcpkg.exe' if os.name=='nt' else 'vcpkg')
 if not exe.exists():
  if getattr(a,'build_tools',None):raise SystemExit('Prepared vcpkg executable is missing; rerun prepareVcpkg or select a verified installation')
  run(vcpkg/('bootstrap-vcpkg.bat' if os.name=='nt' else 'bootstrap-vcpkg.sh'),'-disableMetrics')
 target=a.target;base=TARGETS[target];triplet='gdal4k-'+target.replace('_','-')
 out=BINARY/'build/engine'/target;out.mkdir(parents=True,exist_ok=True)
 triplets=BINARY/'build/triplets';triplets.mkdir(parents=True,exist_ok=True)
 base_file=next((vcpkg/d/(base+'.cmake') for d in ['triplets','triplets/community'] if (vcpkg/d/(base+'.cmake')).exists()),None)
 if not base_file:raise SystemExit('Missing base triplet: '+base)
 base_relative=base_file.relative_to(vcpkg).as_posix()
 settings=f'include("${{VCPKG_ROOT_DIR}}/{base_relative}")\nset(VCPKG_BUILD_TYPE release)\nset(VCPKG_LIBRARY_LINKAGE static)\nlist(APPEND VCPKG_CMAKE_CONFIGURE_OPTIONS -DCMAKE_POSITION_INDEPENDENT_CODE=ON)\n'
 if target=='web':settings+='set(VCPKG_CXX_FLAGS "-fexceptions")\nset(VCPKG_C_FLAGS "-fexceptions")\n'
 if target.startswith('android'):settings+='set(VCPKG_CMAKE_SYSTEM_VERSION 28)\nset(VCPKG_CRT_LINKAGE static)\nset(VCPKG_CMAKE_CONFIGURE_OPTIONS "${VCPKG_CMAKE_CONFIGURE_OPTIONS};-DANDROID_STL=c++_static")\n'
 if target.startswith('ios'):settings+='set(VCPKG_OSX_DEPLOYMENT_TARGET 15.0)\n'
 if target.startswith('macos'):settings+='set(VCPKG_OSX_DEPLOYMENT_TARGET 11.0)\n'
 if target.startswith('linux'):
  # All target dependencies are static, with tools disabled. They have no ELF RPATH.
  settings+='set(VCPKG_FIXUP_ELF_RPATH OFF)\n'
  settings+='if(PORT STREQUAL "gdal")\n  list(APPEND VCPKG_CMAKE_CONFIGURE_OPTIONS -DGDAL_OBJECT_LIBRARIES_POSITION_INDEPENDENT_CODE=ON)\nendif()\n'
  if platform.system()=='Darwin':
   # Pinned Meson helper compares CPU families but misses a same-CPU OS change.
   settings+='set(IS_CROSS TRUE)\n'
 chain=None
 if target.startswith(('linux','windows')):
  chain=native_toolchain(target,out/'toolchain.cmake')
  settings+=f'set(VCPKG_CHAINLOAD_TOOLCHAIN_FILE "${{CMAKE_CURRENT_LIST_DIR}}/../engine/{target}/toolchain.cmake")\n'
 settings=prepare_sdk_cache_inputs(ROOT,vcpkg,out,base_file,chain,target,settings,VCPKG_REV)
 (triplets/(triplet+'.cmake')).write_text(settings)
 env=os.environ.copy();env['VCPKG_MAX_CONCURRENCY']=str(a.jobs)
 if target.startswith('android'):
  env.setdefault('ANDROID_NDK_HOME',str(ROOT/'verification/tools/android-ndk-r28c'))
 installed=out/'installed'
 run(exe,'install','--x-wait-for-lock',f'--x-manifest-root={BINARY/"engine"}',f'--overlay-ports={BINARY/"ports"}',f'--overlay-triplets={triplets}',f'--triplet={triplet}',f'--x-install-root={installed}',*work_options,'--disable-metrics',env=env)
 extra=out/'toolchain-licenses'
 if extra.exists():
  notice=installed/triplet/'share/gdal4k-toolchain/copyright';notice.parent.mkdir(parents=True,exist_ok=True)
  notice.write_text('\n\n'.join(f.name+'\n'+f.read_text(errors='replace') for f in sorted(extra.iterdir())))
 build=out/'cmake' 
 cmd=[cmake,'--fresh','-S',ROOT/'gdal4k/native','-B',build,'-G','Ninja',f'-DCMAKE_MAKE_PROGRAM={ninja}',f'-DCMAKE_TOOLCHAIN_FILE={vcpkg/"scripts/buildsystems/vcpkg.cmake"}',f'-DVCPKG_INSTALLED_DIR={installed}',f'-DVCPKG_TARGET_TRIPLET={triplet}',f'-DVCPKG_OVERLAY_TRIPLETS={triplets}',f'-DVCPKG_OVERLAY_PORTS={BINARY/"ports"}',f'-DVCPKG_MANIFEST_DIR={BINARY/"engine"}','-DVCPKG_INSTALL_OPTIONS='+';'.join(work_options),'-DCMAKE_BUILD_TYPE=Release']
 manifest=json.loads((BINARY/'engine/vcpkg.json').read_text())
 gdal=next(value for value in manifest['dependencies'] if isinstance(value,dict) and value.get('name')=='gdal')
 cmd+=['-DGDAL4K_ENABLE_HTTP='+('ON' if 'curl' in gdal.get('features',[]) else 'OFF')]
 if target.startswith(('linux','windows')):cmd += [f'-DVCPKG_CHAINLOAD_TOOLCHAIN_FILE={chain}']
 if target=='web':
  emsdk=Path(os.environ['EMSDK'])
  cmd += [f'-DVCPKG_CHAINLOAD_TOOLCHAIN_FILE={emsdk/"upstream/emscripten/cmake/Modules/Platform/Emscripten.cmake"}', '-DCMAKE_SYSTEM_NAME=Emscripten','-DCMAKE_CXX_FLAGS=-fexceptions','-DCMAKE_C_FLAGS=-fexceptions']
 if target.startswith('android'):
  ndk=Path(os.environ.get('ANDROID_NDK_HOME',ROOT/'verification/tools/android-ndk-r28c'))
  cmd += ['-DCMAKE_SYSTEM_NAME=Android',f'-DVCPKG_CHAINLOAD_TOOLCHAIN_FILE={ndk/"build/cmake/android.toolchain.cmake"}',f'-DCMAKE_ANDROID_NDK={ndk}','-DANDROID_PLATFORM=android-28','-DANDROID_STL=c++_static',f'-DANDROID_ABI={target[8:]}']
 if target.startswith('ios'):cmd+=['-DCMAKE_SYSTEM_NAME=iOS',f'-DCMAKE_OSX_SYSROOT={"iphonesimulator" if "simulator" in target else "iphoneos"}','-DCMAKE_OSX_ARCHITECTURES=arm64','-DCMAKE_OSX_DEPLOYMENT_TARGET=15.0']
 if target.startswith('macos'):cmd += ['-DCMAKE_OSX_ARCHITECTURES=arm64','-DCMAKE_OSX_DEPLOYMENT_TARGET=11.0']
 run(*cmd,env=env);run(cmake,'--build',build,'--parallel',a.jobs,env=env)
 if target=='web':
  shutil.copy2(build/'gdal4k.mjs',out/'gdal4k.mjs')
  shutil.copy2(build/'gdal4k.wasm',out/'gdal4k.wasm')
  run(os.sys.executable,BINARY/'tools/package-web.py',out/'gdal4k.mjs')
 else:
  # Merge the bridge and its actual static dependency closure into one relocatable archive.
  output='gdal4k.dll' if target.startswith('windows') else 'libgdal4k.dylib' if target.startswith(('macos','ios')) else 'libgdal4k.so'
  arguments=ninja_link_arguments(ninja,build,output)
  archives=[build/'libgdal4k-bridge.a']
  for token in arguments:
   if token.endswith('.a'):
    f=Path(token);f=f if f.is_absolute() else build/f
    if f.is_relative_to(installed) and f not in archives:archives.append(f)
  # Static Apple frameworks have no .a suffix and appear as -framework in Ninja.
  for location in (build/'gdal4k-static-dependencies.txt').read_text().splitlines():
   f=Path(location).resolve()
   if not f.is_relative_to(installed) or f.read_bytes()[:8]!=b'!<arch>\n':raise RuntimeError('Expected an embedded static archive: '+str(f))
   if f not in archives:archives.append(f)
  for required in ['libgdal.a','libproj.a','libjxl.a']:
   if not any(f.name==required for f in archives):raise RuntimeError('Static dependency missing from the Native archive: '+required)
  print('Bundled static closure: '+', '.join(f.name for f in archives),flush=True)
  # Consumers link system libraries with their own target toolchain.
  system_options=list(dict.fromkeys(t for t in arguments if re.fullmatch(r'-l[\w.+-]+',t)))
  # Keep each Apple framework pair together: downstream .distinct() must not
  # discard the second '-framework'. These are system frameworks, not archives.
  for index,token in enumerate(arguments):
   framework=arguments[index+1] if token=='-framework' and index+1<len(arguments) else token[len('-framework '):] if token.startswith('-framework ') else None
   if framework and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]+',framework) and framework.lower()!='tiff':
    option='-framework '+framework
    if option not in system_options:system_options.append(option)
  if target.startswith('windows'):
   # K/N supplies static winpthread with --whole-archive. Re-adding its dynamic
   # import library before that runtime produces duplicate pthread definitions.
   system_options=[t for t in system_options if t not in ['-lwinpthread','-lpthread','-lstdc++']]
  (out/'native-linker-options.json').write_text(json.dumps(system_options)+'\n')
  merged=out/'libgdal4k.a'
  if target.startswith(('macos','ios')):run('/usr/bin/libtool','-static','-o',merged,*archives)
  else:
   ar=(build/'gdal4k-archiver.txt').read_text().strip()
   script='CREATE '+str(merged)+'\n'+''.join('ADDLIB '+str(f)+'\n' for f in archives)+'SAVE\nEND\n'
   run(ar,'-M',input=script,text=True)
  if target.startswith('android'):
   dest=BINARY/'build/android-runtime/jniLibs'/target[8:]/'libgdal4k.so'
  elif not target.startswith('ios'):
   name='gdal4k.dll' if target.startswith('windows') else 'libgdal4k.dylib' if target.startswith('macos') else 'libgdal4k.so'
   dest=BINARY/'build/jvm-resources/gdal4k'/target/name
  else:dest=None
  if dest:
   dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(build/dest.name,dest)
  # License texts travel with every platform artifact.
 licenses=out/'licenses';licenses.mkdir(exist_ok=True)
 for f in (installed/triplet/'share').glob('*/copyright'):
  shutil.copy2(f,licenses/(f.parent.name+'.txt'))
 if target=='web':shutil.copy2(ROOT/'gdal4k/native/serial-viewshed/LICENSE',licenses/'gdal4k-serial-viewshed.txt')
 artifacts=[out/'gdal4k.mjs',out/'gdal4k.wasm'] if target=='web' else [out/'libgdal4k.a',out/'native-linker-options.json']
 if target=='web':artifacts += [p for p in (BINARY/'build/generated/web/assets').rglob('*') if p.is_file()]
 elif dest:artifacts.append(dest)
 artifacts+=list(licenses.glob('*.txt'))
 hashes={f.relative_to(BINARY/'build').as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in artifacts}
 if fingerprint()!=source_hash:raise RuntimeError('Engine sources changed during the build; rebuild before publishing')
 (out/'manifest.json').write_text(json.dumps({'target':target,'source_fingerprint':source_hash,'vcpkg':VCPKG_REV,'gdal':'3.12.4','jxl':True,'artifacts':hashes},indent=2)+'\n')
@contextmanager
def build_lock(path):
 path.parent.mkdir(parents=True,exist_ok=True)
 with path.open('a+b') as lock:
  if os.name=='nt':
   import msvcrt,time
   lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
   while True:
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1);break
    except OSError:time.sleep(.25)
  else:
   import fcntl
   fcntl.flock(lock,fcntl.LOCK_EX)
  try:yield
  finally:
   if os.name=='nt':
    lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
   else:fcntl.flock(lock,fcntl.LOCK_UN)

if __name__=='__main__':
 with build_lock(BINARY/'build/.engine-build.lock'):
  main()

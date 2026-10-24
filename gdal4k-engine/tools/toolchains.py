"""Producer-only toolchain configuration; published consumers use embedded artifacts."""
import os
import platform
import shutil
import subprocess
from pathlib import Path

KOTLIN_VERSION = '2.4.21'

def mingw_header_overlay(gcc, directory):
    """Keep the pinned GCC/MinGW headers usable with K/N's current Clang.

    https://gcc.gnu.org/legacy-ml/libstdc++/2019-11/msg00097.html
    Move filesystem_error before u8path uses it. Definitions and ABI are unchanged.
    The old MinGW intrin.h also redeclares three SSE3 functions that Clang's
    x86intrin.h already defines as constexpr. Keep those declarations for GCC;
    Clang uses its existing definitions without increasing the target CPU level.
    Only this private producer overlay changes, never the user's K/N SDK.
    """
    text=(gcc/'include/c++/9.2.0/bits/fs_path.h').read_text()
    begin=text.index('  class filesystem_error : public std::system_error\n')
    end=text.index('  struct path::_Cmpt',begin)
    definition=text[begin:end]
    text=text[:begin]+text[end:]
    position=text.index('  template<typename _InputIterator>\n    inline auto\n    u8path(')
    text=text[:position]+definition+text[position:]
    intrinsics=(gcc/'x86_64-w64-mingw32/include/intrin.h').read_text()
    for declaration in (
        '    __MACHINEX86X(__m128d _mm_movedup_pd(__m128d))\n',
        '    __MACHINEX86X(__m128 _mm_movehdup_ps(__m128))\n',
        '    __MACHINEX86X(__m128 _mm_moveldup_ps(__m128))\n',
    ):
        if intrinsics.count(declaration)!=1:
            raise RuntimeError('Unexpected pinned MinGW intrinsic declaration: '+declaration.strip())
        intrinsics=intrinsics.replace(declaration,'#if !defined(__clang__)\n'+declaration+'#endif\n')
    destination=directory/'bits/fs_path.h';destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(text)
    (directory/'intrin.h').write_text(intrinsics)
    return directory

def konan_home():
    configured = os.environ.get('KONAN_HOME')
    if configured:
        return Path(configured)
    host = {'Darwin': 'macos', 'Linux': 'linux', 'Windows': 'windows'}[platform.system()]
    arch = 'aarch64' if platform.machine().lower() in ('aarch64', 'arm64') else 'x86_64'
    directory = Path(os.environ.get('KONAN_DATA_DIR', Path.home()/'.konan'))
    home = directory/f'kotlin-native-prebuilt-{host}-{arch}-{KOTLIN_VERSION}'
    if not home.exists():
        raise RuntimeError(f'Initialize the pinned Kotlin/Native compiler first with a gdal4k native compilation task; expected {home}')
    return home

def native_toolchain(target, destination):
    """Use the consumer's C++ ABI and Linux sysroot, not the producer OS's glibc."""
    home = konan_home()
    konan_target = 'mingw_x64' if target.startswith('windows') else 'linux_arm64' if target.endswith('arm64') else 'linux_x64'
    compiler = home/'bin'/('konanc.bat' if os.name == 'nt' else 'konanc')
    subprocess.run([str(compiler), '-Xcheck-dependencies', '-target', konan_target], check=True)
    dependencies = Path(os.environ.get('KONAN_DATA_DIR', Path.home()/'.konan'))/'dependencies'
    if target.startswith('windows'):
        if os.name != 'nt':
            raise RuntimeError('Build the Windows engine on Windows with the Kotlin/Native MinGW toolchain')
        gcc = dependencies/'msys2-mingw-w64-x86_64-2'
        os.environ['PATH'] = str(gcc/'bin')+os.pathsep+os.environ['PATH']
        llvm = next(p for p in dependencies.glob('llvm-21-x86_64-windows-*') if (p/'bin/clang.exe').exists())
        headers=mingw_header_overlay(gcc,destination.parent/'compiler-headers')
        # K/N's stripped GCC distribution has headers/runtime but no cc1plus.
        # Use its LLVM compiler and the matching MinGW sysroot, as K/N itself does.
        # This GCC runtime uses emulated TLS, including std::call_once's internal
        # variables. Match K/N's clangFlags.mingw_x64 for every dependency too.
        text = f'''set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_C_COMPILER "{(llvm/'bin/clang.exe').as_posix()}")
set(CMAKE_CXX_COMPILER "{(llvm/'bin/clang++.exe').as_posix()}")
set(CMAKE_C_COMPILER_TARGET x86_64-w64-windows-gnu)
set(CMAKE_CXX_COMPILER_TARGET x86_64-w64-windows-gnu)
set(CMAKE_SYSROOT "{gcc.as_posix()}")
set(CMAKE_C_FLAGS_INIT "--target=x86_64-w64-windows-gnu --sysroot={gcc.as_posix()} -femulated-tls")
set(CMAKE_CXX_FLAGS_INIT "--target=x86_64-w64-windows-gnu --sysroot={gcc.as_posix()} -femulated-tls -isystem {headers.as_posix()}")
set(CMAKE_EXE_LINKER_FLAGS_INIT "--ld-path={llvm.as_posix()}/bin/ld.lld.exe -pthread")
set(CMAKE_SHARED_LINKER_FLAGS_INIT "--ld-path={llvm.as_posix()}/bin/ld.lld.exe -pthread")
set(CMAKE_RC_COMPILER "{(gcc/'bin/windres.exe').as_posix()}")
set(CMAKE_AR "{(llvm/'bin/llvm-ar.exe').as_posix()}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
'''
    else:
        arch = 'aarch64' if target.endswith('arm64') else 'x86_64'
        version = '2.25' if arch == 'aarch64' else '2.19'
        gcc = dependencies/f'{arch}-unknown-linux-gnu-gcc-8.3.0-glibc-{version}-kernel-4.9-2'
        host = 'macos' if platform.system() == 'Darwin' else 'linux'
        host_arch = 'aarch64' if platform.machine().lower() in ('aarch64','arm64') else 'x86_64'
        llvm = next(p for p in dependencies.glob(f'llvm-21-{host_arch}-{host}-*') if (p/'bin/clang').exists())
        sysroot = gcc/f'{arch}-unknown-linux-gnu/sysroot'
        text = f'''set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR {arch})
set(CMAKE_C_COMPILER "{(llvm/'bin/clang').as_posix()}")
set(CMAKE_CXX_COMPILER "{(llvm/'bin/clang++').as_posix()}")
set(CMAKE_C_COMPILER_TARGET {arch}-unknown-linux-gnu)
set(CMAKE_CXX_COMPILER_TARGET {arch}-unknown-linux-gnu)
set(CMAKE_SYSROOT "{sysroot.as_posix()}")
set(CMAKE_C_FLAGS_INIT "--target={arch}-unknown-linux-gnu --sysroot={sysroot.as_posix()} --gcc-toolchain={gcc.as_posix()}")
set(CMAKE_CXX_FLAGS_INIT "--target={arch}-unknown-linux-gnu --sysroot={sysroot.as_posix()} --gcc-toolchain={gcc.as_posix()}")
set(CMAKE_EXE_LINKER_FLAGS_INIT "--ld-path={llvm.as_posix()}/bin/ld.lld")
set(CMAKE_SHARED_LINKER_FLAGS_INIT "--ld-path={llvm.as_posix()}/bin/ld.lld")
set(CMAKE_AR "{(llvm/'bin/llvm-ar').as_posix()}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
'''
    # Essentials LLVM distributions expose ranlib through the llvm-ar multicall binary.
    # A host ranlib would corrupt the symbol index of cross-platform static archives.
    ar = llvm/'bin'/('llvm-ar.exe' if os.name == 'nt' else 'llvm-ar')
    ranlib = destination.parent/('llvm-ranlib.exe' if os.name == 'nt' else 'llvm-ranlib')
    if not ranlib.exists():
        if os.name == 'nt':shutil.copy2(ar, ranlib)
        else:ranlib.symlink_to(ar)
    text += f'''set(CMAKE_RANLIB "{ranlib.as_posix()}")
set(CMAKE_C_COMPILER_AR "{ar.as_posix()}")
set(CMAKE_CXX_COMPILER_AR "{ar.as_posix()}")
set(CMAKE_C_COMPILER_RANLIB "{ranlib.as_posix()}")
set(CMAKE_CXX_COMPILER_RANLIB "{ranlib.as_posix()}")
set(CMAKE_ASM_COMPILER "${{CMAKE_C_COMPILER}}")
set(CMAKE_ASM_COMPILER_TARGET "${{CMAKE_C_COMPILER_TARGET}}")
set(CMAKE_ASM_FLAGS_INIT "${{CMAKE_C_FLAGS_INIT}}")
'''
    licenses = destination.parent/'toolchain-licenses'
    licenses.mkdir(parents=True,exist_ok=True)
    roots = [gcc/'share/licenses/gcc-libs',gcc/'share/licenses/gcc',gcc/'share/licenses/libwinpthread']
    for root in roots:
        if root.exists():
            for name in ['COPYING.RUNTIME','COPYING3','COPYING.LIB','COPYING']:
                source = root/name
                if source.is_file():
                    (licenses/(root.name+'-'+name)).write_bytes(source.read_bytes())
    destination.write_text(text)
    return destination

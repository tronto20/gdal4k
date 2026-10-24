"""Build a test-only premain agent inside an independent consumer workspace."""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parent / 'jvm-hang-diagnostics/src/dev/gdal4k/verification/JvmHangDiagnosticsAgent.java'
PREMAIN_CLASS = 'dev.gdal4k.verification.JvmHangDiagnosticsAgent'


def agent_jvm_argument(agent_jar, output_dir, *, interval_seconds=60):
    """Return one JVM argument; a shorter interval is an explicit smoke-test option."""
    if not math.isfinite(interval_seconds) or interval_seconds < 0.01:
        raise ValueError('Diagnostic interval must be finite and at least 0.01 seconds')
    jar = Path(agent_jar).resolve()
    if '=' in str(jar):
        raise ValueError('A javaagent JAR path cannot contain =')
    milliseconds = round(interval_seconds * 1000)
    encoded = base64.urlsafe_b64encode(str(Path(output_dir).resolve()).encode('utf-8')).decode('ascii').rstrip('=')
    return f'-javaagent:{jar}=intervalMillis={milliseconds};output64={encoded}'


def prepare_jvm_hang_diagnostics(consumer_root, *, java_home=None, interval_seconds=60):
    """Compile with JDK21 javac/jar; return jar/output paths and the single jvm_arg.

    This builds no application code and downloads nothing. The caller adds
    result['jvm_arg'] to the Test worker's jvmArgs as one string.
    """
    root = Path(consumer_root).resolve()
    home = java_home or os.environ.get('JAVA_HOME')
    if home is None:
        compiler = shutil.which('javac')
        if compiler is None:
            raise RuntimeError('JDK21 javac and jar are required for JVM hang diagnostics')
        home = Path(compiler).resolve().parent.parent
    home = Path(home).resolve()
    suffix = '.exe' if os.name == 'nt' else ''
    javac, jar = [home / 'bin' / (name + suffix) for name in ['javac', 'jar']]
    if not javac.is_file() or not jar.is_file():
        raise RuntimeError('JDK21 javac and jar are required for JVM hang diagnostics')
    version = subprocess.run([str(javac), '-version'], check=True, capture_output=True, text=True)
    version_text = version.stdout + version.stderr
    if not re.search(r'\bjavac 21(?:\.|\s|$)', version_text):
        raise RuntimeError('JVM hang diagnostics require the selected JDK21: ' + version_text.strip())
    output = root / 'build/test-results/jvm-hang-diagnostics'
    build = root / 'build/jvm-hang-diagnostics-agent'
    build.mkdir(parents=True, exist_ok=True)
    final_jar = build / 'jvm-hang-diagnostics-agent.jar'
    argument = agent_jvm_argument(final_jar, output, interval_seconds=interval_seconds)
    commands = []
    with tempfile.TemporaryDirectory(prefix='compile-', dir=build) as temporary:
        temporary = Path(temporary)
        classes = temporary / 'classes'
        classes.mkdir()
        manifest = temporary / 'MANIFEST.MF'
        manifest.write_text('Manifest-Version: 1.0\nPremain-Class: ' + PREMAIN_CLASS
                            + '\nCan-Redefine-Classes: false\nCan-Retransform-Classes: false\n\n', encoding='utf-8')
        target = temporary / 'agent.jar'
        for command in [[str(javac), '--release', '21', '-d', str(classes), str(SOURCE)],
                        [str(jar), '--create', '--file', str(target), '--manifest', str(manifest), '-C', str(classes), '.']]:
            result = subprocess.run(command, check=True, capture_output=True, text=True)
            commands.append({'command': command, 'stdout': result.stdout, 'stderr': result.stderr})
        os.replace(target, final_jar)
    receipt = {'source': str(SOURCE), 'sourceSha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
               'jar': str(final_jar), 'jarSha256': hashlib.sha256(final_jar.read_bytes()).hexdigest(),
               'javaHome': str(home), 'javacVersion': version_text.strip(),
               'outputDir': str(output), 'intervalMillis': round(interval_seconds * 1000),
               'jvmArg': argument, 'commands': commands}
    receipt_bytes = (json.dumps(receipt, indent=2) + '\n').encode('utf-8')
    (build / 'build-receipt.json').write_bytes(receipt_bytes)
    retained = output / 'agent'
    retained.mkdir(parents=True, exist_ok=True)
    (retained / SOURCE.name).write_bytes(SOURCE.read_bytes())
    (retained / final_jar.name).write_bytes(final_jar.read_bytes())
    (retained / 'build-receipt.json').write_bytes(receipt_bytes)
    bindings = {name: {'bytes': (retained / name).stat().st_size,
                       'sha256': hashlib.sha256((retained / name).read_bytes()).hexdigest()}
                for name in [SOURCE.name, final_jar.name, 'build-receipt.json']}
    (retained / 'artifact-bindings.json').write_text(json.dumps(bindings, indent=2) + '\n', encoding='utf-8')
    print('GDAL4K_JVM_HANG_DIAGNOSTICS_BUILT jar=' + str(final_jar), flush=True)
    return {'agent_jar': final_jar, 'output_dir': output, 'jvm_arg': argument,
            'interval_millis': receipt['intervalMillis'], 'receipt': retained / 'build-receipt.json',
            'retained_dir': retained}

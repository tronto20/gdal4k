"""C21 Maven and independently derived JVM alias semantic gates.

Inputs are registered, completed byte-audit rows, not caller acceptance flags.
Only bounded POM/module/manifest/class metadata is captured. Native payload
identities come from fully audited rows and actual CI producer receipts.
"""
from collections import defaultdict
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import struct
import xml.etree.ElementTree as ET
import cohort_policy_v1 as cohort_policy

GROUP = 'dev.tronto.gdal4k'
GROUP_PATH = 'dev/tronto/gdal4k/'
PUBLIC_JVM = {'gdal4k', 'gdal4k-jvm', 'gdal4k-runtime', 'gdal4k-runtime-jvm'}
PLATFORMS = {
    'macosarm64': ('macos-arm64', 'macosArm64', 'macos_arm64'),
    'linuxx64': ('linux-amd64', 'linuxX64', 'linux_x64'),
    'linuxarm64': ('linux-arm64', 'linuxArm64', 'linux_arm64'),
    'mingwx64': ('windows-amd64', 'mingwX64', 'mingw_x64'),
    'iosarm64': ('ios-arm64', 'iosArm64', 'ios_arm64'),
    'iossimulatorarm64': ('ios-simulator-arm64', 'iosSimulatorArm64', 'ios_simulator_arm64'),
}
SUSPEND_DESC = '(Lkotlin/coroutines/Continuation;)Ljava/lang/Object;'
SUSPEND_SIGNATURE = '(Lkotlin/coroutines/Continuation<-Ldev/gdal4k/runtime/MDArray;>;)Ljava/lang/Object;'


def need(value, message):
    if not value:
        raise ValueError(message)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        need(key not in value, 'Duplicate JSON metadata key: ' + key)
        value[key] = item
    return value


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique)


def _source(root, relative):
    path = Path(root) / relative
    before = os.lstat(path)
    need(stat.S_ISREG(before.st_mode), 'Current source policy is not a regular file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        raw = source.read(256 * 1024 + 1)
        after = os.fstat(source.fileno())
    fields = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_size',
              'st_atime_ns', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_blocks')
    need(len(raw) <= 256 * 1024 and all(getattr(before, k) == getattr(after, k) for k in fields),
         'Source policy changed or exceeds source bound: ' + relative)
    return raw


def _assignments(raw, names):
    module = ast.parse(raw)
    result = {}
    for item in module.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    result[target.id] = ast.literal_eval(item.value)
    need(set(result) == set(names), 'Committed publication policy declarations are incomplete')
    return result


def policy(root):
    paths = ['verification/publication_versions.py', 'verification/jvm_binaries.py',
             'gdal4k-engine/tools/engine_variants.py', 'gdal4k-engine/tools/driver-profile.py',
             'verification/check-jvm-publications.py', 'verification/check-engine-variants.py',
             'verification/driver-profile/report.py', 'gradle.properties',
             'gdal4k-engine/tools/build-engine.py', 'gdal4k-engine/tools/versions.py',
             'gdal4k-engine/tools/package-web.py', 'gdal4k-engine/build.gradle.kts']
    sources = {p: _source(root, p) for p in paths}
    pub = _assignments(sources[paths[0]], ['LIBRARY_TARGETS', 'ENGINE_COORDINATES', 'ANDROID_ENGINE'])
    jvm = _assignments(sources[paths[1]], ['JVM_BINARIES'])['JVM_BINARIES']
    variants = _assignments(sources[paths[2]], ['COORDINATES'])['COORDINATES']
    profile = _assignments(sources[paths[3]], ['PROFILES', 'TARGETS'])
    properties = dict(line.split('=', 1) for line in sources['gradle.properties'].decode().splitlines()
                      if '=' in line and not line.startswith('#'))
    version = properties.get('gdal4kVersion')
    need(isinstance(version, str) and version.endswith('-SNAPSHOT'), 'Current source version is not an explicit snapshot')
    expected = {module + suffix for module in ('gdal4k', 'gdal4k-runtime')
                for suffix in ('', *('-' + target for target in pub['LIBRARY_TARGETS']))}
    expected |= {'gdal4k-engine', pub['ANDROID_ENGINE'], *pub['ENGINE_COORDINATES'].values()}
    need(len(expected) == 32 and len(jvm) == 4, 'Current 32-coordinate/four-classifier policy changed')
    pairs = [(name, target) for name, spec in profile['PROFILES'].items() for target in spec['targets']]
    families = {'profile-' + name + '-' + target: (name, target, False) for name, target in pairs}
    families['profile-cross-combined-linux-arm64'] = ('combined', 'linux-arm64', True)
    need(len(families) == 12, 'Current profile/cross publication source matrix changed')
    vcpkg = _assignments(sources['gdal4k-engine/tools/build-engine.py'], ['VCPKG_REV'])['VCPKG_REV']
    return dict(version=version, expected=expected, jvm=jvm, variants=variants, publication=pub,
                profiles=families, profileTargets=profile['TARGETS'], vcpkg=vcpkg,
                sourcePins={p: dict(bytes=len(b), sha256=hashlib.sha256(b).hexdigest()) for p, b in sources.items()})


def _current_policy(c):
    """Validate the reviewed data contract before interpreting current packages."""
    need(type(c.cohort) is dict and type(c.cohort.get('scope')) is dict and
         type(c.cohort.get('source')) is dict and type(c.cohort.get('packages')) is dict,
         'Maven gate has no validated current cohort data')
    scope, source, packages = c.cohort['scope'], c.cohort['source'], c.cohort['packages']
    need(all(type(scope.get(key)) is str and c.scope.get(key) == scope[key]
             for key in ('head', 'tree', 'base', 'testCheckout')),
         'Maven cohort source scope differs from qualified current observations')
    need(type(source.get('root')) is str and str(c.source_root) == source['root'] and
         all(type(source.get(key)) is int and source[key] > 0
             for key in ('trackedFileCount', 'nativeInputCount', 'portVersion')) and
         source['nativeInputCount'] <= source['trackedFileCount'] and
         type(source.get('defaultEngineFingerprint')) is str and
         re.fullmatch('[0-9a-f]{64}', source['defaultEngineFingerprint']),
         'Maven current source facts are not valid typed intent data')
    p = policy(c.source_root)
    counts = {'defaultCoordinates': len(p['expected']), 'jvmClassifiers': len(p['jvm']),
              'profileFamilies': len(p['profiles']),
              'jvmSliceCoordinates': len(PUBLIC_JVM | {'gdal4k-engine', p['publication']['ANDROID_ENGINE']})}
    need(all(type(packages.get(key)) is int and packages[key] == count for key, count in counts.items()),
         'Committed publication package policy differs from reviewed cohort data')
    return p


def _api_method_specs(c):
    specs = c.cohort.get('apiMethods')
    cohort_policy.validate_api_methods(specs)
    return specs


def _engine_fingerprint(root, version):
    """Current committed build-engine.fingerprint algorithm over readonly source files."""
    root = Path(root)
    names = []
    def walk(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOATIME)
        try:
            with os.scandir(fd) as entries:
                children = [(entry.name, entry.stat(follow_symlinks=False)) for entry in entries]
        finally:
            os.close(fd)
        for name, info in children:
            if name == '__pycache__':
                continue
            current = path / name
            if stat.S_ISDIR(info.st_mode):
                walk(current)
            else:
                need(stat.S_ISREG(info.st_mode), 'Unexpected nonregular current engine source')
                names.append(current.relative_to(root).as_posix())
    for relative in ('gdal4k/native', 'gdal4k-engine/ports', 'gdal4k-engine/engine',
                     'gdal4k-engine/tools', 'gdal4k-engine/web'):
        walk(root / relative)
    digest = hashlib.sha256(version.encode())
    # The committed algorithm sorts each of the five directories separately.
    for prefix in ('gdal4k/native/', 'gdal4k-engine/ports/', 'gdal4k-engine/engine/',
                   'gdal4k-engine/tools/', 'gdal4k-engine/web/'):
        for name in sorted(n for n in names if n.startswith(prefix)):
            raw = _source(root, name)
            digest.update(name.encode())
            digest.update(raw.replace(b'\r\n', b'\n'))
    return digest.hexdigest()


def _read(c, records, max_file_bytes=2 * 1024 ** 2):
    records = list(records)
    if not records:
        return {}
    need(len({c.key(r) for r in records}) == len(records), 'Ambiguous repeated captured record key')
    need(all(type(r['bytes']) is int and 0 <= r['bytes'] <= max_file_bytes for r in records),
         'Selected Maven metadata exceeds bounded capture size')
    # Partition independently bounded captures; no aggregate unbounded JSON arena.
    result, batch, charge = {}, [], 0
    for record in records:
        if batch and charge + record['bytes'] > 8 * 1024 ** 2:
            result.update(c.read_many(batch, max_file_bytes=max_file_bytes, max_total_bytes=8 * 1024 ** 2))
            batch, charge = [], 0
        batch.append(record)
        charge += record['bytes']
    if batch:
        result.update(c.read_many(batch, max_file_bytes=max_file_bytes, max_total_bytes=8 * 1024 ** 2))
    need(set(result) == {c.key(r) for r in records}, 'Captured Maven metadata key set differs')
    for record in records:
        raw = result[c.key(record)]
        need(isinstance(raw, bytes) and len(raw) == record['bytes'] and
             hashlib.sha256(raw).hexdigest() == record['sha256'], 'Captured metadata differs from completed byte audit')
    return result


def _repo_component(name):
    before, separator, after = name.partition(GROUP_PATH)
    if not separator:
        return None
    prefix = before.rstrip('/')
    if prefix not in ('maven', 'verification/build/consumer-maven') and not prefix.endswith('/source/verification/build/consumer-maven'):
        return None
    parts = after.split('/')
    if len(parts) == 3 and all(parts):
        return parts[0], parts[1], parts[2], after
    if len(parts) == 2 and parts[1].startswith('maven-metadata'):
        return parts[0], None, parts[1], after
    return None


class Package:
    def __init__(self, c, family):
        self.family = family
        self.files, self.repo_files, self.children = {}, {}, defaultdict(dict)
        for record in c.records(family=family):
            chain = tuple(record['chain'])
            if not chain:
                continue
            found = None
            for position, part in enumerate(chain):
                candidate = _repo_component(part)
                if candidate is not None:
                    found = position, candidate
                    break
            if found is None:
                continue
            position, (module, version, filename, relative) = found
            if position == len(chain) - 1:
                need(relative not in self.repo_files, 'Ambiguous repeated Maven physical file in ' + family)
                self.repo_files[relative] = record
                if version is not None:
                    key = module, version, filename
                    need(key not in self.files, 'Duplicate Maven coordinate file')
                    self.files[key] = record
            else:
                key = module, version, filename
                inner = chain[position + 1:]
                # Large Native/JS IR trees remain in the sealed byte-audit
                # JSONL. This gate retains only the exact leaf maps it uses.
                if len(inner) != 1:
                    continue
                keep = (module in ('gdal4k-jvm', 'gdal4k-runtime-jvm') and filename == f'{module}-{version}.jar')
                keep |= module == 'gdal4k-engine' and filename.endswith('.jar')
                keep |= module == 'gdal4k-engine-android-arm64' and filename.endswith('.aar')
                keep |= module in ('gdal4k-engine-js', 'gdal4k-engine-wasm-js') and filename == f'{module}-{version}.klib'
                keep |= (module.startswith('gdal4k-engine-native-') and filename.endswith('-cinterop-engine.klib') and
                         (inner[0] == 'default/manifest' or inner[0].endswith('/included/libgdal4k.a')))
                if not keep:
                    continue
                need(inner not in self.children[key], 'Duplicate Maven nested member identity')
                self.children[key][inner] = record
        need(self.files, 'No actual Maven publication files in family ' + family)

    def artifact(self, module, version, suffix):
        key = module, version, module + '-' + version + suffix
        need(key in self.files, 'Required Maven artifact is missing: ' + '/'.join(key))
        return key, self.files[key]

    def direct(self, key):
        return {path[0]: record for path, record in self.children[key].items() if len(path) == 1}

    def class_map(self, module, version):
        key, jar = self.artifact(module + '-jvm', version, '.jar')
        children = self.direct(key)
        need(children, 'JVM publication has no audited members')
        need(not any(name.startswith('gdal4k/') or name.endswith(('.so', '.dll', '.dylib', '.a', '.wasm'))
                     for name in children), 'API/runtime JVM JAR contains native engine payload')
        classes = {name: (record['bytes'], record['sha256']) for name, record in children.items() if name.endswith('.class')}
        need(classes, 'JVM publication has no actual class byte map')
        return classes, children


def _xml(raw):
    need(not re.search(br'<!\s*(DOCTYPE|ENTITY)\b', raw, re.I), 'DTD/entity declarations are outside Maven metadata contract')
    return ET.fromstring(raw)


def _target(variant):
    attrs = variant.get('attributes', {})
    platform = attrs.get('org.jetbrains.kotlin.platform.type')
    if platform == 'jvm':
        return 'android' if attrs.get('org.gradle.jvm.environment') == 'android' else 'jvm'
    if platform == 'androidJvm':
        return 'android'
    if platform == 'wasm':
        return 'wasmJs' if attrs.get('org.jetbrains.kotlin.wasm.target') == 'js' else None
    if platform == 'native':
        return next((item[1] for item in PLATFORMS.values() if item[2] == attrs.get('org.jetbrains.kotlin.native.target')), None)
    return platform


def _root_module(module):
    return 'gdal4k-engine' if module.startswith('gdal4k-engine') else 'gdal4k-runtime' if module.startswith('gdal4k-runtime') else 'gdal4k'


def _safe_payload_basename(value, description):
    need(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,511}', value) and
         not value.endswith('.') and value.split('.', 1)[0].casefold() not in
         {'con', 'prn', 'aux', 'nul', *('com' + str(i) for i in range(1, 10)),
          *('lpt' + str(i) for i in range(1, 10))},
         'Gradle payload ' + description + ' is not a safe bounded basename')
    return value


def _validate_package(c, package, p, expected, version, *, complete=False, target=None):
    coordinates = {module for module, _, _ in package.files}
    need(coordinates == set(expected), 'Maven coordinate set differs in ' + package.family)
    need({v for _, v, _ in package.files} == {version}, 'Maven publication versions are mixed or replace default coordinates')
    descriptors = []
    for module in sorted(expected):
        for suffix in ('.pom', '.module'):
            descriptors.append(package.artifact(module, version, suffix)[1])
    captured = _read(c, descriptors)
    namespace = '{http://maven.apache.org/POM/4.0.0}'
    variant_files, redirects, partial_redirects, metadata_targets = 0, 0, 0, {}
    for module in sorted(expected):
        _, pom_record = package.artifact(module, version, '.pom')
        pom = _xml(captured[c.key(pom_record)])
        need((pom.findtext(namespace + 'groupId'), pom.findtext(namespace + 'artifactId'), pom.findtext(namespace + 'version')) ==
             (GROUP, module, version), 'POM coordinate differs from physical publication path')
        for dependency in pom.findall('.//' + namespace + 'dependency'):
            group, name, dep_version = (dependency.findtext(namespace + key) for key in ('groupId', 'artifactId', 'version'))
            if group == GROUP:
                need(dep_version == version and name in p['expected'], 'POM internal dependency uses a foreign version or coordinate')
            need(not (module in PUBLIC_JVM and name and name.startswith('gdal4k-engine')),
                 'Public JVM POM adds an implicit engine dependency')
        _, module_record = package.artifact(module, version, '.module')
        metadata = _json(captured[c.key(module_record)])
        component = metadata.get('component', {})
        need(component.get('group') == GROUP and component.get('version') == version and
             component.get('module') in (module, _root_module(module)), 'Gradle target/root component identity differs')
        variants = metadata.get('variants')
        need(isinstance(variants, list) and variants and len(variants) <= 128, 'Missing or excessive Gradle variant inventory')
        need(len({v.get('name') for v in variants}) == len(variants), 'Duplicate Gradle variant name')
        seen_targets, payload_targets, jvm_usages = set(), set(), set()
        for variant in variants:
            need(isinstance(variant, dict) and isinstance(variant.get('attributes'), dict), 'Invalid Gradle variant shape')
            selected = _target(variant)
            library = variant['attributes'].get('org.gradle.category') == 'library'
            if library:
                seen_targets.add(selected)
            if variant['name'].startswith('empty'):
                need(not any(variant.get(k) for k in ('files', 'dependencies', 'available-at', 'dependencyConstraints')),
                     'Empty engine variant contains payload or dependency')
            elif library and selected != 'common':
                payload_targets.add(selected)
            if module == 'gdal4k-engine' and selected == 'jvm' and library:
                need(not any(variant.get(k) for k in ('files', 'dependencies', 'available-at', 'dependencyConstraints')),
                     'Engine root chooses a default JVM binary')
                jvm_usages.add(variant['attributes'].get('org.gradle.usage'))
            for dependency in variant.get('dependencies', []):
                need(isinstance(dependency, dict), 'Invalid Gradle dependency')
                name, group = dependency.get('module'), dependency.get('group')
                if group == GROUP:
                    need(name in p['expected'] and dependency.get('version', {}).get('requires') == version,
                         'Gradle internal dependency version or coordinate differs')
                need(not (module in PUBLIC_JVM and name and name.startswith('gdal4k-engine')),
                     'Public JVM Gradle metadata adds an implicit engine dependency')
            redirect = variant.get('available-at')
            if redirect is not None:
                name = redirect.get('module')
                expected_redirect = (p['variants'].get(selected) if module == 'gdal4k-engine' else
                                     _root_module(module) + '-' + ('wasm-js' if selected == 'wasmJs' else str(selected).lower()))
                need(name == expected_redirect and redirect.get('group') == GROUP and redirect.get('version') == version and
                     redirect.get('url') == f'../../{name}/{version}/{name}-{version}.module',
                     'Gradle available-at target coordinate/path differs')
                need(not variant.get('files'), 'Gradle redirect also contains an ambiguous direct payload')
                if name in expected:
                    package.artifact(name, version, '.module')
                    redirects += 1
                else:
                    need(not complete, 'Complete Maven graph has an absent target redirect')
                    partial_redirects += 1
            for payload in variant.get('files', []):
                need(isinstance(payload, dict), 'Invalid Gradle artifact reference')
                _safe_payload_basename(payload.get('name'), 'logical name')
                url = _safe_payload_basename(payload.get('url'), 'URL')
                key = module, version, url
                need(key in package.files, 'Gradle variant names a missing physical payload')
                actual = package.files[key]
                need(type(payload.get('size')) is int and payload['size'] == actual['bytes'] and
                     payload.get('sha256') == actual['sha256'], 'Gradle payload bytes/SHA256 differ from actual archive member')
                variant_files += 1
        metadata_targets[module] = sorted(str(t) for t in payload_targets)
        if module == 'gdal4k-engine':
            need(jvm_usages == {'java-api', 'java-runtime'}, 'Engine root omits explicit empty JVM API/runtime choices')
            wanted = set(p['variants'])
            need((wanted | {'common'}) <= seen_targets and payload_targets == wanted,
                 'Engine root target coverage/payload selection differs from committed policy')
        elif module.startswith('gdal4k-engine-'):
            wanted_target = next((t for t, name in p['variants'].items() if name == module), None)
            need(wanted_target is not None and payload_targets == {wanted_target}, 'Split engine module payload target differs')
    need(variant_files > 0 and redirects > 0, 'No actual Gradle payload/target joins were performed')
    return dict(family=package.family, coordinateCount=len(coordinates), coordinates=sorted(coordinates), version=version,
                gradlePayloadSizeAndSha256Joins=variant_files, boundAvailableAtTargets=redirects,
                explicitlyOutOfPartialSliceRedirects=partial_redirects, engineMetadataTargets=metadata_targets)


def _record_identity(record):
    return record['bytes'], record['sha256']


def _one(records, message):
    records = list(records)
    need(len(records) == 1, message)
    return records[0]


def _default_anchors(c, p):
    engine_families = {value[0]: 'engine-' + value[1] for value in PLATFORMS.values()}
    engine_families.update({'web': 'engine-web', 'android-arm64-v8a': 'engine-android-arm64-v8a'})
    anchors, fingerprints = {}, set()
    for platform, family in engine_families.items():
        need(family in c.families, 'Current producer engine family is missing: ' + family)
        rows = {}
        for record in c.records(family=family):
            name = record['chain'][-1]
            if name.startswith('gdal4k-engine/build/'):
                need(name not in rows, 'Ambiguous actual producer engine file')
                rows[name] = record
        manifest_path = 'gdal4k-engine/build/engine/' + platform + '/manifest.json'
        need(manifest_path in rows, 'Current producer manifest is missing')
        manifest_record = rows[manifest_path]
        manifest = _json(_read(c, [manifest_record])[c.key(manifest_record)])
        need(manifest.get('target') == platform and manifest.get('gdal') == '3.12.4' and manifest.get('jxl') is True and
             manifest.get('vcpkg') == p['vcpkg'] and
             re.fullmatch('[0-9a-f]{64}', manifest.get('source_fingerprint', '')),
             'Current producer engine manifest target/version/fingerprint differs')
        artifacts = manifest.get('artifacts')
        need(isinstance(artifacts, dict) and artifacts, 'Producer manifest has no artifact hashes')
        for name, sha in artifacts.items():
            path = 'gdal4k-engine/build/' + name
            need(path in rows and rows[path]['sha256'] == sha, 'Producer artifact differs from its actual manifest hash')
        fingerprints.add(manifest['source_fingerprint'])
        options_name = 'gdal4k-engine/build/engine/' + platform + '/native-linker-options.json'
        options = None
        if options_name in rows:
            options = _json(_read(c, [rows[options_name]])[c.key(rows[options_name])])
            need(isinstance(options, list) and all(isinstance(x, str) for x in options), 'Invalid producer system linker options')
        anchors[platform] = dict(family=family, manifest=manifest, manifestRecord=manifest_record,
                                 artifactRows=rows, hashes=artifacts, linkerOptions=options,
                                 producerByteIdentitiesIndependentlyObserved=True)
    need(len(fingerprints) == 1, 'Default producer engine source fingerprints differ across platforms')
    actual = next(iter(fingerprints))
    need(actual == _engine_fingerprint(c.source_root, p['version']), 'Actual default engine fingerprint differs from current H17 source bytes')
    return anchors, actual


def _profile_report(c, family, name, target):
    windows = target == 'windows-amd64'
    prefix = 'build/driver-profiles/w/' if windows else f'build/driver-profiles/{name}/{target}/'
    record = _one((r for r in c.find_records(family=family, suffix='report.json')
                   if r['chain'][-1].startswith(prefix) and '/source/' not in r['chain'][-1] and
                   (not windows or re.fullmatch(re.escape(prefix) + '[0-9a-f]{24}/report\\.json', r['chain'][-1]))),
                  'Expected exactly one actual profile publication report')
    report = _json(_read(c, [record])[c.key(record)])
    need(report.get('profile') == name and report.get('target') == target,
         'Profile report scope differs from selected publication family')
    if windows:
        identity = report.get('sourceInputs')
        need(isinstance(identity, dict) and identity.get('profile') == name and identity.get('target') == target and
             isinstance(report.get('version'), str) and identity.get('version') == report['version'],
             'Windows profile report input identity scope differs')
        # Exact H17 prepare() serialization: default separators/ensure_ascii,
        # sorted keys and no newline. The sourceBindings predecessor separately
        # binds all615 source hashes and the complete prepared transform map.
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        need(record['chain'][-1] == prefix + digest[:24] + '/report.json',
             'Windows profile report directory differs from its complete input identity digest')
    report_source = _source(c.source_root, 'verification/driver-profile/report.py')
    need(report.get('sourceInputs', {}).get('sources', {}).get('verification/driver-profile/report.py') ==
         hashlib.sha256(report_source).hexdigest(), 'Profile producer hash assertions are not bound to current report source')
    return report, record


def _cross_anchor(c, family):
    suffix = '/source/gdal4k-engine/build/engine/linux-arm64/manifest.json'
    record = _one((r for r in c.find_records(family=family, suffix='manifest.json') if r['chain'][-1].endswith(suffix)),
                  'Cross-produced profile engine manifest is missing or ambiguous')
    manifest = _json(_read(c, [record])[c.key(record)])
    prefix = record['chain'][-1][:-len('engine/linux-arm64/manifest.json')]
    rows = {r['chain'][-1]: r for r in c.records(family=family) if r['chain'][-1].startswith(prefix)}
    need(manifest.get('target') == 'linux-arm64' and manifest.get('gdal') == '3.12.4' and manifest.get('jxl') is True,
         'Cross profile producer manifest target/version differs')
    for name, sha in manifest.get('artifacts', {}).items():
        need(prefix + name in rows and rows[prefix + name]['sha256'] == sha, 'Cross producer bytes differ from manifest')
    return dict(family=family, hashes=manifest['artifacts'], manifest=manifest, manifestRecord=record,
                artifactRows=rows, linkerOptions=None, producerByteIdentitiesIndependentlyObserved=True)


def _profile_anchor(c, package, p, name, target, cross):
    if cross:
        return _cross_anchor(c, package.family), None
    report, record = _profile_report(c, package.family, name, target)
    measured = report.get('measured', {}).get('profile', {})
    need(isinstance(measured.get('artifactHashes'), dict) and
         re.fullmatch('[0-9a-f]{64}', measured.get('fingerprint', '')), 'Profile producer engine hash anchor is missing')
    need(isinstance(report.get('archive'), str) and '/' not in report['archive'] and
         re.fullmatch('[0-9a-f]{64}', report.get('archiveSha256', '')), 'Invalid profile Maven archive identity')
    archive_record = _one((r for r in c.find_records(family=package.family, suffix=report['archive'])
                           if r['chain'][-1].endswith('/' + report['archive'])), 'Profile Maven archive receipt is missing or ambiguous')
    need(archive_record['sha256'] == report.get('archiveSha256'), 'Profile report archive digest differs from actual compressed member')
    publication = report.get('publicationBindings', {})
    expected = {GROUP_PATH + rel: {'bytes': r['bytes'], 'sha256': r['sha256']} for rel, r in package.repo_files.items()}
    need(publication.get('artifactSha256') == expected, 'Profile publication file identity map differs from audited actual Maven bytes')
    return dict(family=package.family, hashes=measured['artifactHashes'], linkerOptions=None,
                manifestRecord=record, manifest=dict(source_fingerprint=measured['fingerprint']),
                producerByteIdentitiesIndependentlyObserved=False,
                producerAnchorScope='Actual CI current-source report hashes joined to audited published bytes; direct producer libraries are not uploaded for this family'), report


def _linker_properties(raw):
    text = raw.decode('utf-8')
    result = {}
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        need(separator and key not in result, 'Malformed/duplicate KLIB manifest property')
        result[key] = value
    need('libraryPaths' not in result, 'Published KLIB contains a producer library path')
    tokens = shlex.split(result.get('linkerOpts', ''))
    cursor = 0
    while cursor < len(tokens):
        if tokens[cursor] == '-framework':
            need(cursor + 1 < len(tokens) and re.fullmatch('[A-Za-z][A-Za-z0-9_]+', tokens[cursor + 1]),
                 'Invalid system framework linker option')
            cursor += 2
        else:
            need(re.fullmatch(r'-l[\w.+-]+', tokens[cursor]), 'Published KLIB contains non-system/producer linker options')
            cursor += 1
    return result


def _payload_bindings(c, package, p, version, anchors, *, targets=None):
    comparisons = []
    targets = set(anchors) if targets is None else set(targets)
    allowed_jni_jars = {f'gdal4k-engine-{version}-{classifier}.jar'
                        for classifier, (_, platform, _) in p['jvm'].items() if platform in targets}
    for (module, observed_version, filename), members in package.children.items():
        if module != 'gdal4k-engine' or observed_version != version:
            continue
        carries_engine = any(path[0].startswith('gdal4k/') or path[0].endswith(('.so', '.dylib', '.dll', '.a'))
                             for path in members)
        need(not carries_engine or filename in allowed_jni_jars,
             'Foreign/unselected JVM classifier contains an engine payload')
    for classifier, (_, platform, filename) in p['jvm'].items():
        if platform not in targets:
            continue
        key, jar = package.artifact('gdal4k-engine', version, '-' + classifier + '.jar')
        children = package.direct(key)
        resource = f'gdal4k/{platform}/{filename}'
        need(set(children) - {'META-INF/MANIFEST.MF'} == {resource}, 'JVM classifier JAR has mixed/missing resource payloads')
        anchor = anchors[platform]
        producer_path = 'jvm-resources/' + resource
        need(children[resource]['sha256'] == anchor['hashes'].get(producer_path), 'Classifier JNI bytes differ from producer anchor')
        if anchor['producerByteIdentitiesIndependentlyObserved']:
            rows = anchor['artifactRows']
            matched = [r for name, r in rows.items() if name.endswith('/' + producer_path)]
            need(matched and all(_record_identity(r) == _record_identity(children[resource]) for r in matched),
                 'Classifier JNI byte length/SHA differ from actual producer bytes')
        comparisons.append(dict(kind='jvm-classifier', platform=platform, classifier=classifier,
                                bytes=children[resource]['bytes'], sha256=children[resource]['sha256'],
                                producerFamily=anchor['family'], directProducerBytes=anchor['producerByteIdentitiesIndependentlyObserved']))
    if 'web' in targets and all(module in {coordinate for coordinate, _, _ in package.files}
                                for module in ('gdal4k-engine-js', 'gdal4k-engine-wasm-js')):
        anchor = anchors['web']
        marker = 'generated/web/assets/'
        assets = {path[len(marker):]: sha for path, sha in anchor['hashes'].items() if path.startswith(marker)}
        fd = os.open(Path(c.source_root) / 'gdal4k-engine/web/runtime',
                     os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOATIME)
        try:
            with os.scandir(fd) as entries:
                runtime_names = {entry.name for entry in entries if entry.name.endswith('.mjs')}
        finally:
            os.close(fd)
        expected_names = runtime_names | {'gdal4k.mjs', 'gdal4k.wasm', 'gdal4k-data.wasm',
                                          'gdal4k-resources.mjs', 'package.json'}
        need(assets and set(assets) == expected_names, 'Producer web asset manifest differs from current package-web/runtime source policy')
        for module in ('gdal4k-engine-js', 'gdal4k-engine-wasm-js'):
            key, klib = package.artifact(module, version, '.klib')
            children = package.direct(key)
            observed_names = {name for name in children if name.endswith(('.mjs', '.wasm')) or name == 'package.json'}
            need(observed_names == expected_names, 'Published JS/Wasm KLIB asset inventory differs from current producer')
            bound = []
            for name in sorted(expected_names):
                producer_path = 'gdal4k-engine/build/' + marker + name
                producer = anchor['artifactRows'].get(producer_path)
                need(producer is not None and children[name]['sha256'] == assets[name] and
                     _record_identity(children[name]) == _record_identity(producer),
                     'Published JS/Wasm asset bytes/SHA256 differ from actual producer asset')
                if name.endswith('.wasm'):
                    need(children[name].get('details', {}).get('format') == 'WASM' and
                         children[name]['details'].get('version') == 1,
                         'Published web asset does not retain its ordinary Wasm header')
                bound.append(dict(path=name, bytes=children[name]['bytes'], sha256=children[name]['sha256']))
            package_record = children['package.json']
            package_json = _json(_read(c, [package_record])[c.key(package_record)])
            need(package_json.get('name') == '@gdal4k/web-engine' and package_json.get('version') == version and
                 package_json.get('type') == 'module' and package_json.get('main') == 'index.mjs',
                 'Published web package entrypoint/name/version differs from current producer')
            comparisons.append(dict(kind='web-klib-assets', module=module, producerFamily=anchor['family'],
                                    assetCount=len(bound), assets=bound))
    if 'android-arm64-v8a' in targets:
        key, aar = package.artifact(p['publication']['ANDROID_ENGINE'], version, '.aar')
        children = package.direct(key)
        native = {name: r for name, r in children.items() if name.startswith('jni/') and name.endswith('.so')}
        resource = 'jni/arm64-v8a/libgdal4k.so'
        need(set(native) == {resource}, 'Android engine AAR has missing/foreign ABI libraries')
        anchor = anchors['android-arm64-v8a']
        wanted = 'android-runtime/jniLibs/arm64-v8a/libgdal4k.so'
        need(native[resource]['sha256'] == anchor['hashes'].get(wanted), 'Android AAR JNI differs from actual producer manifest')
        producer = anchor['artifactRows'].get('gdal4k-engine/build/' + wanted)
        need(producer is not None and _record_identity(producer) == _record_identity(native[resource]), 'Android AAR actual producer bytes/size differ')
        comparisons.append(dict(kind='android-aar', bytes=native[resource]['bytes'], sha256=native[resource]['sha256']))
    for suffix, (platform, _, native_target) in PLATFORMS.items():
        if platform not in targets:
            continue
        module = p['publication']['ENGINE_COORDINATES'][suffix]
        if module not in {coordinate for coordinate, _, _ in package.files}:
            # The deliberately partial six-coordinate JVM slice has no Native
            # target modules. Its four JNI JARs and Android AAR remain required.
            continue
        key, klib = package.artifact(module, version, '-cinterop-engine.klib')
        children = package.direct(key)
        included = [(name, r) for name, r in children.items() if name.endswith('/included/libgdal4k.a')]
        need(len(included) == 1, 'Native engine KLIB has missing/ambiguous embedded static archive')
        name, archive = included[0]
        anchor = anchors[platform]
        wanted = f'engine/{platform}/libgdal4k.a'
        need(archive['sha256'] == anchor['hashes'].get(wanted), 'Published Native archive differs from current producer anchor')
        if anchor['producerByteIdentitiesIndependentlyObserved']:
            matched = [r for path, r in anchor['artifactRows'].items() if path.endswith('/' + wanted)]
            need(matched and all(_record_identity(r) == _record_identity(archive) for r in matched), 'Native archive actual producer bytes/size differ')
        need('default/manifest' in children, 'Native engine KLIB manifest is missing')
        manifest_record = children['default/manifest']
        props = _linker_properties(_read(c, [manifest_record])[c.key(manifest_record)])
        need(props.get('native_targets') == native_target, 'Published KLIB native target differs from current source route')
        exact_options = anchor.get('linkerOptions')
        if exact_options is not None:
            cxx = [] if platform.startswith('windows') else ['-lc++'] if platform.startswith(('macos', 'ios')) else ['-lstdc++']
            expected = ' '.join(dict.fromkeys(cxx + exact_options))
            need(props.get('linkerOpts') == expected, 'Published KLIB system options differ from actual producer JSON')
        comparisons.append(dict(kind='native-klib', platform=platform, bytes=archive['bytes'], sha256=archive['sha256'],
                                producerFamily=anchor['family'], directProducerBytes=anchor['producerByteIdentitiesIndependentlyObserved'],
                                exactProducerLinkerJsonObserved=exact_options is not None))
    return comparisons


def _profile_expected(p, target):
    native = p['profileTargets'][target][2].lower()
    return {module + suffix for module in ('gdal4k', 'gdal4k-runtime') for suffix in ('', '-jvm', '-' + native)} | \
        {'gdal4k-engine', p['publication']['ENGINE_COORDINATES'][native]}


def _class_digest(mapping):
    return hashlib.sha256(json.dumps(mapping, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _compiled_report_matches(report, maps):
    expected = {module + '/build/classes/kotlin/jvm/main/' + name: sha
                for module, mapping in maps.items() for name, (_, sha) in mapping.items()}
    need(report.get('publicationBindings', {}).get('compiledProductionSha256') == expected,
         'Actual published JVM class map differs from current CI producer-class hash map')
    return len(expected)


def _published_api_methods(raw, expected_class, method_specs):
    """Bounded JVM structural parser; selects the published suspend API method."""
    need(len(raw) <= 2 * 1024 ** 2 and raw[:4] == b'\xca\xfe\xba\xbe', 'Published API class is missing or malformed')
    offset = 8
    def take(count):
        nonlocal offset
        need(0 <= count <= len(raw) - offset, 'Truncated JVM class structure')
        value = raw[offset:offset + count]
        offset += count
        return value
    def u2():
        return int.from_bytes(take(2), 'big')
    def u4():
        return int.from_bytes(take(4), 'big')
    count = u2()
    pool, index = {}, 1
    while index < count:
        tag = take(1)[0]
        if tag == 1:
            pool[index] = take(u2())
        elif tag in (3, 4):
            take(4)
        elif tag in (5, 6):
            take(8)
            index += 1
        elif tag == 7:
            pool[index] = ('class', u2())
        elif tag in (8, 16, 19, 20):
            take(2)
        elif tag in (9, 10, 11, 12, 17, 18):
            take(4)
        elif tag == 15:
            take(3)
        else:
            raise ValueError('Unsupported JVM constant-pool tag')
        index += 1
    take(2)
    this_class = pool.get(u2())
    need(isinstance(this_class, tuple) and pool.get(this_class[1]) == expected_class.encode(), 'Published class identity differs')
    take(2)
    take(2 * u2())
    def attributes():
        values = {}
        for _ in range(u2()):
            name = pool.get(u2())
            size = u4()
            value = take(size)
            if name == b'Signature':
                need(size == 2 and name not in values, 'Invalid JVM method generic signature')
                values[name] = pool.get(int.from_bytes(value, 'big'))
        return values
    for _ in range(u2()):
        take(6)
        attributes()
    cohort_policy.validate_api_methods(method_specs)
    selected = {item['logicalId']: [] for item in method_specs}
    need(len(selected) == len(method_specs), 'Duplicate current API logical contract')
    targets = {}
    for item in method_specs:
        key = tuple(value.encode() for value in cohort_policy.method_contract_key(item))
        need(key not in targets, 'Duplicate current API JVM method target')
        targets[key] = item['logicalId']
    for _ in range(u2()):
        flags, name, descriptor = u2(), pool.get(u2()), pool.get(u2())
        attrs = attributes()
        logical_id = targets.get((name, descriptor))
        if logical_id is not None:
            selected[logical_id].append((flags, descriptor, attrs.get(b'Signature')))
    attributes()
    need(offset == len(raw), 'Trailing JVM class structural bytes')
    result = []
    for item in method_specs:
        methods = selected[item['logicalId']]
        need(len(methods) == 1 and methods[0][0] & 1 and not methods[0][0] & 8 and
             methods[0][1] == item['descriptor'].encode() and methods[0][2] == item['genericSignature'].encode(),
             'Published ' + item['logicalId'] + ' public instance suspend descriptor/generic return differs')
        result.append(dict(className=expected_class, logicalId=item['logicalId'], method=item['name'],
                           parameterTypes=item['parameterTypes'], returnType=item['returnType'],
                           actualJvmName=item['jvmName'], descriptor=item['descriptor'],
                           genericSignature=item['genericSignature'], wireKey=item['wireKey'],
                           contractRole=item['contractRole']))
    return result


def gate_maven(c):
    c.require('sourceBindings' in c.prior, 'Maven gate requires the completed current sourceBindings predecessor')
    p = _current_policy(c)
    _api_method_specs(c)
    anchors, fingerprint = _default_anchors(c, p)
    need(fingerprint == c.cohort['source']['defaultEngineFingerprint'],
         'Current producer fingerprint differs from reviewed current source data')
    complete = Package(c, 'gdal4k-maven')
    full_result = _validate_package(c, complete, p, p['expected'], p['version'], complete=True)
    full_result['producerPayloadBindings'] = _payload_bindings(c, complete, p, p['version'], anchors)
    subset = Package(c, 'gdal4k-jvm-maven')
    subset_coordinates = PUBLIC_JVM | {'gdal4k-engine', p['publication']['ANDROID_ENGINE']}
    jvm_result = _validate_package(c, subset, p, subset_coordinates, p['version'])
    need(all(key in complete.repo_files and _record_identity(record) == _record_identity(complete.repo_files[key])
             for key, record in subset.repo_files.items()), 'JVM Maven slice bytes differ from complete current repository')
    jvm_result['producerPayloadBindings'] = _payload_bindings(c, subset, p, p['version'], anchors,
                                                            targets=set(p['jvm'][k][1] for k in p['jvm']) | {'android-arm64-v8a'})
    profiles = []
    for family, (name, target, cross) in sorted(p['profiles'].items()):
        c.require(family in c.families, 'Current profile package family is absent: ' + family)
        package = Package(c, family)
        version = p['version'][:-9] + '-' + name + '-SNAPSHOT'
        result = _validate_package(c, package, p, _profile_expected(p, target), version, target=target)
        anchor, report = _profile_anchor(c, package, p, name, target, cross)
        result['producerPayloadBindings'] = _payload_bindings(c, package, p, version, {target: anchor}, targets={target})
        maps = {module: package.class_map(module, version)[0] for module in ('gdal4k', 'gdal4k-runtime')}
        if report is not None:
            result['actualCIProducerClassHashJoins'] = _compiled_report_matches(report, maps)
        else:
            result['actualCIProducerClassHashJoins'] = None
            result['crossCompileOnlyPublicationScope'] = True
        profiles.append(result)
    return dict(schemaVersion=1, gate='maven', result='passed', publicationPolicySourcePins=p['sourcePins'],
                defaultVersion=p['version'],
                complete=full_result, jvmSlice=jvm_result, profilePackages=profiles,
                defaultEngineSourceFingerprint=fingerprint, completeCoordinatesObserved=len(p['expected']),
                profilePackageFamiliesObserved=len(profiles), explicitJvmClassifiersObserved=len(p['jvm']),
                previousCohortClassBaselineUsed=False, externalPublicationEstablished=False,
                scope='Actual archived Maven graph and byte identity; runtime and final CI scope are separate gates')


def gate_maven_alias_independent(c):
    c.require('maven' in c.prior, 'Independent alias gate requires the genuinely executed Maven predecessor')
    p = _current_policy(c)
    method_specs = _api_method_specs(c)
    need(c.prior['maven'].get('defaultEngineSourceFingerprint') == c.cohort['source']['defaultEngineFingerprint'],
         'Independent alias predecessor used a different current native source')
    complete = Package(c, 'gdal4k-maven')
    baseline, required_records = {}, []
    for module, class_name in [('gdal4k', 'dev/gdal4k/runtime/MDArray.class'),
                               ('gdal4k-runtime', 'dev/gdal4k/internal/BundledMDArray.class')]:
        mapping, children = complete.class_map(module, p['version'])
        need(class_name in children, 'Published current API/implementation class is absent')
        baseline[module] = mapping
        required_records.append(children[class_name])
    captured = _read(c, required_records)
    signatures = [signature for record in required_records
                  for signature in _published_api_methods(captured[c.key(record)], record['chain'][-1][:-6], method_specs)]
    need(len(signatures) == 2 * len(method_specs) and
         len({(row['className'], row['logicalId']) for row in signatures}) == len(signatures),
         'Published current API logical method coverage differs')
    contract_counts = {role: sum(item['contractRole'] == role for item in method_specs)
                       for role in ('feature', 'legacyCompatibility')}
    signature_counts = {role: sum(item['contractRole'] == role for item in signatures)
                        for role in contract_counts}
    need(all(signature_counts[role] == 2 * count for role, count in contract_counts.items()),
         'Published feature/legacy compatibility coverage differs')
    comparisons = []
    all_families = [('gdal4k-jvm-maven', p['version'], None)]
    all_families += [(family, p['version'][:-9] + '-' + name + '-SNAPSHOT', (name, target, cross))
                     for family, (name, target, cross) in sorted(p['profiles'].items())]
    for family, version, profile in all_families:
        package = Package(c, family)
        need({v for _, v, _ in package.files} == {version}, 'Independent alias version isolation differs')
        maps = {}
        for module in ('gdal4k', 'gdal4k-runtime'):
            actual, _ = package.class_map(module, version)
            need(actual == baseline[module], 'Published API/runtime JVM class bytes differ across current aliases')
            maps[module] = actual
        compiled_count = None
        if profile is not None and not profile[2]:
            report, _ = _profile_report(c, family, profile[0], profile[1])
            compiled_count = _compiled_report_matches(report, maps)
        comparisons.append(dict(family=family, version=version,
                                classMaps={module: dict(classCount=len(mapping), canonicalMapSha256=_class_digest(mapping))
                                           for module, mapping in maps.items()},
                                actualCIProducerClassHashJoins=compiled_count))
    return dict(schemaVersion=1, gate='mavenAliasIndependent', result='passed',
                sourcePolicyPins=p['sourcePins'], currentPublishedSuspendMethodSignatures=signatures,
                currentLogicalContractCounts=dict(total=len(method_specs), **contract_counts),
                currentPublishedSignatureCounts=dict(total=len(signatures), **signature_counts),
                baselineFamily='gdal4k-maven', baselineVersion=p['version'],
                baselineClassMaps={module: dict(classCount=len(mapping), canonicalMapSha256=_class_digest(mapping))
                                   for module, mapping in baseline.items()},
                independentlyDerivedComparisons=comparisons,
                previousCohortClassBaselineUsed=False,
                scope='Current actual CI published bytes and CI producer hash maps; no previous-cohort class baseline inheritance')

"""Public test-only runtime fixture contract; never includes authentication material."""
import json
import re
import hashlib
from pathlib import Path

ENVIRONMENT_VARIABLE = 'GDAL4K_TEST_DRIVER_FIXTURE'
MARKER = ENVIRONMENT_VARIABLE + ' '
KEYS = {'schemaVersion', 'profile', 'target', 'runId', 'postgresqlDsn', 'httpUrls', 'hdf5Path'}
URL_KEYS = {'http', 'https', 'untrusted', 'wrong-name'}


def validate(value):
    """Return an independent validated public mapping, without echoing rejected values."""
    if not isinstance(value, dict) or set(value) != KEYS:
        raise ValueError('Unexpected test runtime fixture keys')
    if type(value['schemaVersion']) is not int or value['schemaVersion'] != 1:
        raise ValueError('Invalid test runtime fixture schema')
    if value['profile'] != 'combined' or value['target'] != 'linux-arm64':
        raise ValueError('Test runtime fixture profile or target mismatch')
    if not isinstance(value['runId'], str) or not re.fullmatch(r'[a-z][a-z0-9_-]{15,79}', value['runId']):
        raise ValueError('Invalid test runtime fixture runId')
    dsn = value['postgresqlDsn']
    pg = re.fullmatch(r'PG:host=127\.0\.0\.1 port=([1-9][0-9]{0,4}) '
                      r'dbname=([a-z][a-z0-9_]{0,62}) user=\2', dsn) if isinstance(dsn, str) else None
    if not pg or not 0 < int(pg[1]) < 65536:
        raise ValueError('Invalid public test PostgreSQL endpoint')
    urls = value['httpUrls']
    if not isinstance(urls, dict) or set(urls) != URL_KEYS:
        raise ValueError('Invalid test HTTP origin inventory')
    ports = set()
    for name, url in urls.items():
        scheme = 'http' if name == 'http' else 'https'
        match = re.fullmatch(scheme + r'://127\.0\.0\.1:([1-9][0-9]{0,4})', url) if isinstance(url, str) else None
        if not match or not 0 < int(match[1]) < 65536:
            raise ValueError('Invalid test HTTP origin')
        ports.add(int(match[1]))
    if len(ports) != 4:
        raise ValueError('Test fixture servers must use distinct ports')
    path = value['hdf5Path']
    if not isinstance(path, str) or not path.startswith('/') or any(c in path for c in '\0\r\n"'):
        raise ValueError('Invalid absolute test HDF5 path')
    return dict(value, httpUrls=dict(urls))


def canonical_json(value):
    return json.dumps(validate(value), sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def parse(value):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise ValueError('Invalid test runtime fixture JSON') from None
    return validate(parsed)


def copy_to_consumer(snapshot, consumer, target):
    """Copy only generated test helpers; default publications have no such helpers."""
    source = Path(snapshot) / 'gdal4k-runtime/src'
    common = source / 'commonTest/kotlin/dev/gdal4k/DriverProfileRuntimeFixture.kt'
    if not common.is_file():
        return []
    native_targets = {'linuxX64', 'linuxArm64', 'macosArm64', 'mingwX64', 'iosArm64', 'iosSimulatorArm64'}
    if target != 'jvm' and target not in native_targets:
        raise ValueError('Unsupported runtime fixture consumer target')
    platform = 'Jvm' if target == 'jvm' else 'Native'
    actual = source / f'{"jvmTest" if target == "jvm" else "nativeTest"}/kotlin/dev/gdal4k/DriverProfileRuntimeFixture{platform}.kt'
    planned = []
    for original, source_set in [(common, 'commonTest'), (actual, target + 'Test')]:
        if original.is_symlink() or not original.is_file():
            raise ValueError('Missing regular generated runtime fixture helper')
        data = original.read_bytes()
        text = data.decode('utf-8')
        if text.count('package dev.gdal4k\n') != 1:
            raise ValueError('Unexpected runtime fixture helper package')
        copied = text.replace('package dev.gdal4k\n', 'package consumer\n').encode('utf-8')
        destination = Path(consumer) / f'src/{source_set}/kotlin/consumer' / original.name
        planned.append((destination, copied, dict(source=original.relative_to(snapshot).as_posix(),
                                                consumer=destination.relative_to(consumer).as_posix(),
                                                sourceSha256=hashlib.sha256(data).hexdigest(),
                                                consumerSha256=hashlib.sha256(copied).hexdigest())))
    receipt = []
    for destination, copied, item in planned:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(copied)
        receipt.append(item)
    return receipt


def preserve_consumer_sources(consumer, destination, receipt):
    """Retain the actual generated test bytes beside compile/runtime evidence."""
    for item in receipt:
        source = Path(consumer) / item['consumer']
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != item['consumerSha256']:
            raise ValueError('Generated runtime fixture consumer source changed')
        output = Path(destination) / 'fixture-sources' / item['consumer']
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        item['preservedSource'] = output.relative_to(destination).as_posix()

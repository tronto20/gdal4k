#!/usr/bin/env python3
"""Run Node/Chromium related-file regression using only a published engine KLIB."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile

root = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo', required=True, type=Path)
parser.add_argument('--target', required=True, choices=['js', 'wasmJs'])
args = parser.parse_args()
suffix = 'js' if args.target == 'js' else 'wasm-js'
klibs = list((args.repo.resolve() / f'dev/tronto/gdal4k/gdal4k-engine-{suffix}').glob('*/*.klib'))
assert len(klibs) == 1, 'Expected exactly one published web engine KLIB'
with tempfile.TemporaryDirectory(prefix='gdal4k-published-related-') as temporary:
    assets = Path(temporary)
    with zipfile.ZipFile(klibs[0]) as archive:
        required = {'index.mjs', 'gdal4k-worker.mjs', 'gdal4k-inputs.mjs', 'gdal4k-http-input.mjs', 'gdal4k.wasm'}
        assert required.issubset(archive.namelist()), 'Missing published input modules'
        for name in archive.namelist():
            if '/' not in name and (name.endswith(('.mjs', '.wasm')) or name == 'package.json'):
                (assets / name).write_bytes(archive.read(name))
    subprocess.run(['node', str(root / 'verification/related-inputs.mjs'), str(assets), args.target], cwd=root, check=True)
result = root / f'verification/results/related-inputs-{args.target}/web.json'
value = json.loads(result.read_text())
assert value['status'] == 'passed' and value['platforms'] == ['Node', 'Chromium']
value.update(target=args.target, published_assets=True, klib_sha256=hashlib.sha256(klibs[0].read_bytes()).hexdigest())
result.write_text(json.dumps(value, indent=2) + '\n')
print('GDAL4K_PUBLISHED_RELATED_INPUTS_OK', args.target)

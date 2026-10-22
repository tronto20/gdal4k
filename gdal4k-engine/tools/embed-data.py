#!/usr/bin/env python3
"""Generate portable C arrays for the complete GDAL data directory and notices."""
import argparse
import json
from pathlib import Path
from gdal_resources import resource_files

p = argparse.ArgumentParser()
p.add_argument('prefix', type=Path)
p.add_argument('output', type=Path)
p.add_argument('--web', action='store_true', help='Web resources are read on demand from the packaged resource data asset')
a = p.parse_args()
root = a.prefix/'share/gdal'
files = resource_files(a.prefix)
notices = '\n\n'.join('=== '+f.parent.name+' ===\n'+f.read_text(encoding='utf-8') for f in sorted((a.prefix/'share').glob('*/copyright')))
if a.web:
    notices += '\n\n=== gdal4k serial Viewshed ===\n' + (Path(__file__).resolve().parents[2]/'gdal4k/native/serial-viewshed/LICENSE').read_text(encoding='utf-8')
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text('// GDAL data bytes live in the lazily read web resource asset.\n'
        'const char* gdal4k_data_directory() { return "/vsigdal4k/resources"; }\n'
        'const char* gdal4k_license_notices() { return '+json.dumps(notices,ensure_ascii=True)+'; }\n'
        'void gdal4k_install_resources() {}\n', encoding='utf-8', newline='\n')
    raise SystemExit(0)
lines = ['// Generated from the pinned GDAL distribution.','''#include "resources.h"
#include <cpl_vsi.h>
#include <cpl_conv.h>
#include <cstring>
#include <stdexcept>
#include <string>
''']
for i,f in enumerate(files):
    data = f.read_bytes()
    lines.append(f'static const unsigned char resource_{i}[] = {{')
    lines.extend(','.join(str(v) for v in data[j:j+32])+',' for j in range(0,len(data),32))
    lines.append('};')
lines.append('struct Resource { const char* name; const unsigned char* data; size_t size; };\nstatic const Resource resources[] = {')
for i,f in enumerate(files):
    lines.append('{'+json.dumps(f.relative_to(root).as_posix())+f', resource_{i}, sizeof(resource_{i})'+'},')
lines.append('};')
# Quote UTF-8 notice text as C++ strings after normalizing CRLF/CR to LF.
lines.append('static const char notices[] = '+json.dumps(notices,ensure_ascii=True)+';')
lines.append('''const char* gdal4k_data_directory() { return "/vsimem/gdal4k-resources/gdal"; }
const char* gdal4k_license_notices() { return notices; }
void gdal4k_install_resources() {
    for (const auto& entry : resources) {
        const auto path = std::string(gdal4k_data_directory()) + "/" + entry.name;
        VSIMkdirRecursive(path.substr(0,path.find_last_of('/')).c_str(),0700);
        auto bytes = static_cast<GByte*>(CPLMalloc(entry.size));
        std::memcpy(bytes,entry.data,entry.size);
        auto file = VSIFileFromMemBuffer(path.c_str(),bytes,entry.size,true);
        if(!file) throw std::runtime_error("Cannot mount embedded GDAL resources");
        VSIFCloseL(file);
    }
}
''')
a.output.parent.mkdir(parents=True,exist_ok=True)
a.output.write_text('\n'.join(lines)+'\n', encoding='utf-8', newline='\n')

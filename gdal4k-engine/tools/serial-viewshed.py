#!/usr/bin/env python3
"""Produce the pinned GDAL 3.12.4 Viewshed executor without worker threads."""
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]);destination=Path(sys.argv[2])
for name,expected in json.loads((root/'sha256.json').read_text()).items():
    if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:
        raise SystemExit('Unexpected GDAL Viewshed source: '+name)
code=(root/'viewshed_executor.cpp').read_text()
assert code.count('m_pool(4)')==1
assert code.count('CPLJobQueuePtr pQueue = m_pool.CreateJobQueue();')==3
assert code.count('pQueue->SubmitJob(')==6
assert code.count('pQueue->WaitCompletion();')==2
code=code.replace('m_pool(4)','m_pool()')
code=code.replace('CPLJobQueuePtr pQueue = m_pool.CreateJobQueue();','')
code=code.replace('pQueue->SubmitJob(', 'gdal4kSerialViewshedJob(')
code=code.replace('pQueue->WaitCompletion();','')
code='// Generated serial scheduling variant of GDAL 3.12.4. Calculation is unchanged.\n#include <gdal_version.h>\n#if GDAL_VERSION_NUM != 3120400\n#error Serial Viewshed executor requires pinned GDAL 3.12.4\n#endif\n' + code
code=code.replace('namespace gdal\n{','template <typename F> static void gdal4kSerialViewshedJob(F&& job) { job(); }\n\nnamespace gdal\n{',1)
assert 'SubmitJob' not in code and 'CreateJobQueue' not in code
assert 'm_pool()' in code
destination.write_text(code)

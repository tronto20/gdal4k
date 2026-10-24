"""Execute the real private classifier with pinned upstream sizes/messages.

This host C++ unit needs no SDK, libcurl library or network. It extracts the
unchanged complete CURLcode enum and CURL_ERROR_SIZE from the official header,
then compiles the actual helper from the production header. It verifies byte
classification/privacy only, not HTTP, Schannel or Windows runtime behavior.
"""
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'verification/fixtures/http-error-diagnostics-curl-8.23.0'
ARCHIVE_SHA512 = ('e1003f374675a6ae50463995a6c705208c4247120093f1fcccf030847161235b3d'
                  'fc0e40927114ccbc5b4530893990ccb96ec15400ffe4bcb275d26a0499f10e')
RAW_SHA256 = {
    'curl.h.gz': 'ad8989a81e79b407355e14d673410d51420bfec48287f4b890d0b211d9235ec3',
    'schannel_verify.c.gz': 'b8750a9f6c147d8ed9b137a54a14f6d3206069b58263de17245dd41cb6d4ecf6',
    'COPYING': '82f2f4427d6545ee5aaac4f0b80428da6cc8ba41c2cf5da3a03680ec327b9681',
}


def function_source(source):
    marker = 'inline const char* schannelTrustReason('
    if source.count(marker) != 1:
        raise AssertionError('Expected one real Schannel reason classifier')
    start = source.index(marker)
    body = source.index('{', start)
    depth = 0
    # Ignore strings and comments only to locate the complete original body.
    tokens = re.finditer(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|[{}]', source[body:], re.S)
    for token in tokens:
        if token.group() == '{':
            depth += 1
        elif token.group() == '}':
            depth -= 1
            if depth == 0:
                return source[start:body + token.end()]
    raise AssertionError('Unbalanced classifier source')


def source_messages(schannel):
    # Preserve adjacent C literal concatenation from the complete pinned source.
    section = '\n'.join(schannel.splitlines()[790:803])
    arguments = re.findall(r'failf\(data,\s*((?:"[^"\\]*(?:\\.[^"\\]*)*"\s*)+)\)', section)
    messages = [''.join(json.loads(token) for token in re.findall(r'"[^"\\]*(?:\\.[^"\\]*)*"', value))
                for value in arguments]
    if len(messages) != 5 or len(set(messages)) != 5 or any('%' in value for value in messages):
        raise AssertionError('Expected five complete static upstream trust messages')
    return messages


class HttpErrorDiagnosticsTest(unittest.TestCase):
    def fixture(self):
        provenance = json.loads((FIXTURE / 'provenance.json').read_text())
        self.assertEqual(ARCHIVE_SHA512, provenance['archiveSha512'])
        self.assertEqual('curl-8_23_0', provenance['upstreamRef'])
        self.assertTrue(provenance['originalsUnmodified'])
        contents = {}
        for record in provenance['files']:
            stored = (FIXTURE / record['path']).read_bytes()
            raw = gzip.decompress(stored) if record['path'].endswith('.gz') else stored
            self.assertEqual(record['storedBytes'], len(stored))
            self.assertEqual(record['storedSha256'], hashlib.sha256(stored).hexdigest())
            self.assertEqual(record['rawBytes'], len(raw))
            self.assertEqual(record['rawSha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(RAW_SHA256[record['path']], record['rawSha256'])
            contents[record['path']] = raw.decode('utf-8')
        self.assertEqual({'curl.h.gz', 'schannel_verify.c.gz', 'COPYING'}, set(contents))
        return contents

    def test_exact_upstream_fixture_and_api_contract(self):
        files = self.fixture()
        sizes = re.findall(r'^#define CURL_ERROR_SIZE (\d+)\s*$', files['curl.h.gz'], re.M)
        self.assertEqual(['256'], sizes)
        self.assertEqual(5, len(source_messages(files['schannel_verify.c.gz'])))
        self.assertIn('CURLE_PEER_FAILED_VERIFICATION', files['curl.h.gz'])

    def test_real_helper_rejects_secret_bearing_buffers_and_wrong_curl_codes(self):
        files = self.fixture()
        compiler = shutil.which('c++') or shutil.which('clang++') or shutil.which('g++')
        if not compiler:
            raise RuntimeError('A host C++17 compiler is required for the privacy regression')
        header = files['curl.h.gz']
        begin = header.index('typedef enum {\n  CURLE_OK = 0')
        end = header.index('} CURLcode;', begin) + len('} CURLcode;')
        enum = header[begin:end]  # The exact full original enum, not copied numeric values.
        size = re.findall(r'^#define CURL_ERROR_SIZE (\d+)\s*$', header, re.M)[0]
        helper = function_source((ROOT / 'gdal4k/native/http-inputs.hpp').read_text())
        reasons = ['revoked', 'incomplete-chain', 'untrusted-root', 'not-time-valid', 'revocation-status-unknown']
        entries = ',\n'.join('{' + json.dumps(message) + ', ' + json.dumps(reason) + '}'
                             for message, reason in zip(source_messages(files['schannel_verify.c.gz']), reasons))
        source = ('#include <cstring>\n#include <cstdio>\n#include <string>\n#include <vector>\n'
                  '#include <algorithm>\n#define CURL_ERROR_SIZE ' + size + '\n' + enum + '\n' + helper +
                  '\nint main() {\nstd::vector<std::pair<std::string,std::string>> positives = {' + entries + '};\n')
        source += r'''
    std::vector<std::string> negatives = {
        "", "https://user:secret-native-http-test-token@127.0.0.1/file?secret=token",
        "C:\\owned\\secret-native-http-test-token\\ca.crt", "secret-native-http-test-token",
        std::string(CURL_ERROR_SIZE, 'X')
    };
    for (const auto& entry : positives) {
        negatives.push_back("prefix" + entry.first);
        negatives.push_back(entry.first + "suffix");
        negatives.push_back(entry.first + "\n");
        negatives.push_back("https://host/?secret-native-http-test-token=" + entry.first);
        negatives.push_back(entry.first + ": C:\\private\\ca.crt");
        negatives.push_back("embedded " + entry.first + " in dynamic text");
        negatives.push_back(entry.first + std::string(1, '\0') + "secret-native-http-test-token");
    }
    int index = 0;
    for (const auto& entry : positives) {
        char buffer[CURL_ERROR_SIZE] = {};
        std::memcpy(buffer, entry.first.data(), entry.first.size());
        if (std::strcmp(schannelTrustReason(CURLE_PEER_FAILED_VERIFICATION, buffer), entry.second.c_str())) return 1;
        const CURLcode other[] = {CURLE_OK, CURLE_COULDNT_CONNECT, CURLE_SSL_CONNECT_ERROR,
                                  CURLE_SSL_CACERT_BADFILE, CURLE_RECV_ERROR};
        for (CURLcode code : other) {
            if (std::strcmp(schannelTrustReason(code, buffer), "unavailable")) return 2;
        }
        ++index;
    }
    for (const auto& text : negatives) {
        char buffer[CURL_ERROR_SIZE] = {};
        std::memcpy(buffer, text.data(), std::min(text.size(), sizeof(buffer)));
        if (std::strcmp(schannelTrustReason(CURLE_PEER_FAILED_VERIFICATION, buffer), "unavailable")) return 3;
    }
    std::printf("HTTP_SCHANNEL_ALLOWLIST_PRIVACY_OK positives=%zu negatives=%zu nonPeer=25\n",
                positives.size(), negatives.size());
}
'''
        with tempfile.TemporaryDirectory(prefix='http error privacy ') as temporary:
            directory = Path(temporary)
            path = directory / 'actual-helper.cpp'
            path.write_text(source)
            executable = directory / 'actual-helper'
            compiled = subprocess.run([compiler, '-std=c++17', '-O2', str(path), '-o', str(executable)],
                                      capture_output=True, text=True, timeout=30)
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual('', result.stderr)
            self.assertEqual('HTTP_SCHANNEL_ALLOWLIST_PRIVACY_OK positives=5 negatives=40 nonPeer=25\n', result.stdout)
            for secret in ['secret-native-http-test-token', 'https://', 'ca.crt', 'schannel:']:
                self.assertNotIn(secret, result.stdout)


if __name__ == '__main__':
    unittest.main()

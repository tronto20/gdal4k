package dev.gdal4k

import dev.gdal4k.internal.*
import dev.gdal4k.runtime.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.*
import kotlin.test.*

class FileTransferControlTest {
    private fun metadata(files: String, main: String = "one.bin") = Json.parseToJsonElement(
        """{"binaryFiles":true,"main":"$main","files":$files}""")
    @Test fun independentPayloadPreservesNamesZeroLengthAndByteOwnership() = runTest {
        val bytes = byteArrayOf(-1, 0, 1, 127)
        val result = decodeBinaryFiles(metadata("""[{"name":"one.bin","size":4},{"name":"empty.bin","size":0}]"""), bytes)
        assertContentEquals(byteArrayOf(-1, 0, 1, 127), result.mainFile().bytes)
        assertTrue(result.files.last().bytes.isEmpty()); bytes[0] = 9
        assertEquals(-1, result.mainFile().bytes[0].toInt())
    }
    @Test fun malformedLengthsAndBasenamesFailBeforeReturningAnyBundle() = runTest {
        for (entries in listOf(
            """[{"name":"one.bin","size":-1}]""", """[{"name":"one.bin","size":2147483648}]""",
            """[{"name":"one.bin","size":1.5}]""", """[{"name":"one.bin","size":2}]""",
            """[{"name":"one.bin","size":0}]""", """[{"name":"../one.bin","size":1}]""",
            """[{"name":"one.bin","size":1},{"name":"one.bin","size":0}]""", "[]"
        )) assertFails { decodeBinaryFiles(metadata(entries), byteArrayOf(7)) }
        assertFails { decodeBinaryFiles(metadata("""[{"name":"other.bin","size":1}]"""), byteArrayOf(1)) }
    }
    @Test fun legacyEngineUsesOriginalNumericAndBase64Methods() = runTest {
        val methods = mutableListOf<String>()
        val engine = RuntimeEngine(object : GdalTransport {
            override val supportsBinaryRaster = true
            override val supportsBinaryCoordinates = true
            override val supportsBinaryColumns = true
            override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
                val args = Json.parseToJsonElement(request).jsonObject["args"]!!.jsonObject
                val method = args["method"]!!.jsonPrimitive.content; methods += method
                val p = args["parameters"]!!.jsonObject
                return when {
                    method.startsWith("writeMemoryFile") -> { assertEquals("[-1,0,1]", p["bytes"].toString()); """{"result":{"value":null}}""" }
                    method.startsWith("readMemoryFile") -> """{"result":{"value":[-1,0,1]}}"""
                    method.startsWith("openDataset") -> { assertEquals("/wAB", p["files"]!!.jsonObject["files"]!!.jsonArray[0].jsonObject["data"]!!.jsonPrimitive.content); """{"result":{"value":"1"}}""" }
                    method.startsWith("translate_") -> """{"result":{"value":{"main":"one.bin","files":[{"name":"one.bin","data":"/wAB"}]}}}"""
                    else -> """{"result":{"value":null}}"""
                }
            }
            override suspend fun close() {}
        })
        val runtime = BundledGdalRuntime(engine); val bytes = byteArrayOf(-1, 0, 1)
        runtime.writeMemoryFile("/vsimem/one.bin", bytes); assertContentEquals(bytes, runtime.readMemoryFile("/vsimem/one.bin"))
        runtime.openDataset(GdalFiles(GdalFile("one.bin", bytes))).use { assertContentEquals(bytes, it.translate("GTiff", "one.bin", emptyList()).mainFile().bytes) }
        assertEquals(5, methods.size); engine.close()
    }
    @Test fun queuedCancelledWriteNeverCallsBinaryTransport() = runTest {
        val entered = CompletableDeferred<Unit>(); val release = CompletableDeferred<Unit>(); var writes = 0
        val engine = RuntimeEngine(object : GdalTransport {
            override val supportsBinaryFiles = true
            override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
                entered.complete(Unit); release.await(); return """{"result":{"value":null}}"""
            }
            override suspend fun callBinary(request: String, input: ByteArray?, control: GdalCallControl?): GdalBinaryReply { writes++; error("unexpected") }
            override suspend fun close() {}
        })
        val first = launch { engine.invoke("Handle", "0", "wait", emptyMap()) }; entered.await()
        val queued = launch(start = CoroutineStart.UNDISPATCHED) { BundledGdalRuntime(engine).writeMemoryFile("/vsimem/wait.bin", ByteArray(524305)) }
        queued.cancel(); release.complete(Unit); first.join(); queued.join()
        assertTrue(queued.isCancelled); assertEquals(0, writes); engine.close()
    }
    @Test fun invalidInputReplyReleasesAllocatedDatasetWithoutDeadlocking() = runTest {
        var closed: String? = null
        val engine = RuntimeEngine(object : GdalTransport {
            override val supportsBinaryFiles = true
            override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
                closed = Json.parseToJsonElement(request).jsonObject["args"]!!.jsonObject["handle"]!!.jsonPrimitive.content
                return """{"result":{"value":null}}"""
            }
            override suspend fun callBinary(request: String, input: ByteArray?, control: GdalCallControl?) =
                GdalBinaryReply("""{"result":{"value":"11"},"allocatedHandles":["11"]}""", byteArrayOf(1))
            override suspend fun close() {}
        })
        assertFails { BundledGdalRuntime(engine).openDataset(GdalFiles(GdalFile("bad.tif", byteArrayOf(1)))) }
        assertEquals("11", closed); engine.close()
    }
}

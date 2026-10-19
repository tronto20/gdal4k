package dev.gdal4k

import dev.gdal4k.internal.*
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import kotlin.test.*

class FileTransferWireTest {
    private fun request(method: String, parameters: String) =
        """{"op":"runtime","args":{"type":"GdalRuntime","handle":"0","method":"$method","parameters":$parameters}}"""

    @Test fun malformedNativeFramesCannotOverwriteExistingFile(): Unit = runBlocking {
        val transport = createTransport(GdalOptions()); assertTrue(transport.supportsBinaryFiles)
        val engine = RuntimeEngine(transport); val runtime = BundledGdalRuntime(engine)
        val path = "/vsimem/file-wire-preserved.bin"
        try {
            val original = byteArrayOf(0, -1, 127); runtime.writeMemoryFile(path, original)
            for (size in listOf("-1", "4", "1.5", "\"3\"", "2147483648")) {
                val reply = transport.callBinary(request("writeMemoryFile_String_ByteArray", """{"path":"$path","byteSize":$size}"""), original, null)
                assertNotNull(Json.parseToJsonElement(reply.json).jsonObject["error"])
                assertContentEquals(original, runtime.readMemoryFile(path))
            }
            val read = transport.callBinary(request("readMemoryFile_String", """{"path":"$path"}"""), byteArrayOf(1), null)
            assertNotNull(Json.parseToJsonElement(read.json).jsonObject["error"])
            assertContentEquals(original, runtime.readMemoryFile(path))
        } finally { runtime.deleteMemoryFile(path); engine.close() }
    }

    @Test fun malformedBundleNamesSizesAndTrailingBytesFailInNative(): Unit = runBlocking {
        val transport = createTransport(GdalOptions()); assertTrue(transport.supportsBinaryFiles)
        val engine = RuntimeEngine(transport)
        try {
            for (entries in listOf(
                """[{"name":"one.bin","size":-1}]""", """[{"name":"one.bin","size":2}]""",
                """[{"name":"one.bin","size":1.5}]""", """[{"name":"one.bin","size":0}]""",
                """[{"name":"../one.bin","size":1}]""", """[{"name":"one.bin","size":1},{"name":"one.bin","size":0}]"""
            )) {
                val parameters = """{"files":{"main":"one.bin","files":$entries},"options":{"options":[],"kind":0,"update":false}}"""
                val reply = transport.callBinary(request("openDataset_GdalFiles_DatasetOpenOptions", parameters), byteArrayOf(1), null)
                val json = Json.parseToJsonElement(reply.json).jsonObject
                assertNotNull(json["error"]); assertTrue(json["allocatedHandles"]?.jsonArray.orEmpty().isEmpty())
                assertTrue(reply.bytes.isEmpty())
            }
            // Method gating cannot turn a direct-path call into a binary input operation.
            assertFails { transport.callBinary(request("translateTo_String_String_List_String", "{}"), byteArrayOf(1), null) }
        } finally { engine.close() }
    }
}

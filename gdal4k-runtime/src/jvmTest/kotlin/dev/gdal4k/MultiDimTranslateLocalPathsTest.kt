package dev.gdal4k

import dev.gdal4k.runtime.*
import java.nio.file.Files
import kotlinx.coroutines.runBlocking
import kotlin.test.*

class MultiDimTranslateLocalPathsTest {
    @Test fun localFileBackedSelectionWritesIndependentNewFileWithoutReplacingExisting(): Unit = runBlocking {
        val runtime = Gdal4k.runtime()
        val directory = Files.createTempDirectory("multidimensional-conversion-")
        val source = directory.resolve("source.vrt")
        val target = directory.resolve("converted.tif")
        val xml = """<VRTDataset><Group name="/"><Dimension name="time" size="2"/><Dimension name="y" size="2"/><Dimension name="x" size="3"/><Array name="cube"><DataType>Int16</DataType><DimensionRef ref="/time"/><DimensionRef ref="/y"/><DimensionRef ref="/x"/><InlineValues>1 2 3 4 5 6 11 12 13 14 15 16</InlineValues></Array></Group></VRTDataset>"""
        try {
            Files.writeString(source, xml)
            runtime.openDataset(source.toString(), DatasetOpenOptions(kind = DatasetKind.Multidimensional)).use { input ->
                input.multiDimTranslateTo(target.toString(), "-array", "name=cube,view=[1,:,:]")
                val before = Files.readAllBytes(target)
                assertFails { input.multiDimTranslateTo(target.toString(), "-array", "name=cube,view=[1,:,:]") }
                assertContentEquals(before, Files.readAllBytes(target))
                assertEquals(xml, Files.readString(source))
            }
            Files.delete(source)
            runtime.openDataset(target.toString()).use { result ->
                assertEquals(3, result.getRasterXSize()); assertEquals(2, result.getRasterYSize())
                val output = IntArray(6); assertEquals(0, result.readRaster(output))
                assertContentEquals(intArrayOf(11, 12, 13, 14, 15, 16), output)
            }
        } finally {
            Files.walk(directory).use { paths -> paths.sorted(Comparator.reverseOrder()).forEach { Files.deleteIfExists(it) } }
        }
    }
}

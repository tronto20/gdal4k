package dev.gdal4k

import dev.gdal4k.GdalException
import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import java.nio.file.Files
import java.nio.file.Path
import kotlin.test.*

class TileIndexLocalPathsTest {
    @Test fun relativePathsArePreservedAndAbsolutePathsResolveAgainstCwd(): Unit = runBlocking {
        val runtime = Gdal4k.runtime()
        val directory = Files.createTempDirectory(Path.of("."), "tile-index-relative-")
        val relative = directory.resolve("raster.tif").toString()
        try {
            requireNotNull(runtime.getDriver("GTiff")).use { driver ->
                requireNotNull(driver.create(relative, 4, 3, 1, GdalDataType.Byte)).use { ds ->
                    assertEquals(0, ds.setGeoTransform(doubleArrayOf(100.0, 2.0, 0.0, 50.0, 0.0, -3.0)))
                }
            }
            for (absolute in listOf(false, true)) {
                runtime.openDataset(runtime.tileIndex(listOf(relative), absolutePaths = absolute)).use { ds ->
                    requireNotNull(ds.getLayer(0)).use { layer -> requireNotNull(layer.getNextFeature()).use { feature ->
                        val actual = feature.getFieldAsString("location")
                        if (absolute) { assertTrue(Path.of(actual).isAbsolute); assertEquals(Path.of(relative).toRealPath(), Path.of(actual).toRealPath()) }
                        else assertEquals(relative, actual)
                    } }
                }
            }
            assertFailsWith<GdalException> { runtime.tileIndex(listOf(directory.toString())) }
            assertTrue(Files.exists(Path.of(relative)))
        } finally { Files.walk(directory).use { paths -> paths.sorted(Comparator.reverseOrder()).forEach { Files.deleteIfExists(it) } } }
    }
}

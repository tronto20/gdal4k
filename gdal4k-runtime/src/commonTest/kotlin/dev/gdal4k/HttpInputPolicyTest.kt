package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*

class HttpInputPolicyTest {
    @Test fun explicitPolicyCannotSilentlyApplyToMemoryOrLocalInputs() = runTest {
        val runtime = Gdal4k.runtime()
        val policy = DatasetOpenOptions(httpInputPolicy = HttpInputPolicy(requestTimeoutMillis = 500))
        requireNotNull(runtime.getDriver("MEM")).use { driver ->
            requireNotNull(driver.create("", 2, 2, 1, GdalDataType.Byte)).use { source ->
                assertEquals(0, source.writeRaster(byteArrayOf(1, 2, 3, 4)))
                val files = source.translate(driver = "GTiff")
                assertFailsWith<IllegalArgumentException> { runtime.openDataset(files, policy) }
                assertFailsWith<IllegalArgumentException> { runtime.openDataset("/vsimem/absent-http-policy.tif", policy) }
                runtime.openDataset(files).use { assertEquals(2, it.getRasterXSize()) }
                assertEquals(2, source.getRasterYSize())
            }
        }
    }
}

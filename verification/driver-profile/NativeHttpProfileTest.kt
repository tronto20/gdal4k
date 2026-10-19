package dev.gdal4k

import dev.gdal4k.GdalException
import dev.gdal4k.runtime.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

/** Added only to the isolated HTTP profile, and copied unchanged to real published consumers. */
class NativeHttpProfileTest {
    private fun url(case: String, suffix: String = "", origin: String = "@HTTP_BASE@") =
        "$origin/$case/$datasetOutputPrefix$suffix/fixture.tif"
    private suspend fun stats(case: String, suffix: String = ""): Map<String, Int> {
        val r = Gdal4k.runtime()
        return r.openDataset("@HTTP_BASE@/stats/$datasetOutputPrefix$suffix/$case.geojson", DatasetOpenOptions(httpInputPolicy = HttpInputPolicy())).use { ds ->
            requireNotNull(ds.getLayer(0)).use { layer -> requireNotNull(layer.getNextFeature()).use { feature ->
                listOf("requests", "bodyBytes", "maxRange", "status503", "status401", "ifRange", "modifiedRange").associateWith { feature.getFieldAsInteger(it) }
            } }
        }
    }
    private suspend fun readLast(ds: Dataset) {
        requireNotNull(ds.getRasterBand(1)).use { band ->
            val pixels = ByteArray(256 * 256)
            assertEquals(0, band.readRaster(BandRasterIOOptions(1792, 1792, 256, 256), pixels))
            for (i in pixels.indices) {
                val x = 1792 + i % 256; val y = 1792 + i / 256
                assertEquals((x * 31 + y * 17 + (x xor y)) and 255, pixels[i].toInt() and 255)
            }
        }
    }
    @Test fun boundedRandomAccessUsesValidatorsAndKeepsVrtLease() = runTest(timeout = 30.seconds) {
        val r = Gdal4k.runtime()
        val source = r.openDataset(url("ok"), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy(requireStrongETag = true)))
        val vrt = r.buildVRT(listOf(source), emptyList())
        source.close()
        vrt.use { readLast(it) }
        val result = stats("ok")
        assertTrue(result.getValue("maxRange") <= 65536)
        assertTrue(result.getValue("bodyBytes") in 65537..1048576)
        assertTrue(result.getValue("ifRange") > 0)
    }
    @Test fun relatedPamSidecarAndMissingOptionalFiles() = runTest(timeout = 30.seconds) {
        Gdal4k.runtime().openDataset(url("sidecar"), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy())).use { ds ->
            assertEquals("present", ds.getMetadataItem("HTTP_SIDECAR")); readLast(ds)
        }
    }
    @Test fun transientRetryAndAuthenticationNoRetry() = runTest(timeout = 30.seconds) {
        val r = Gdal4k.runtime()
        r.openDataset(url("retry"), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy(maxRetries = 2))).use { readLast(it) }
        assertEquals(1, stats("retry").getValue("status503"))
        val error = assertFailsWith<GdalException> {
            r.openDataset(url("denied") + "?secret-native-http-test-token", DatasetOpenOptions(httpInputPolicy = HttpInputPolicy(maxRetries = 2)))
        }
        assertFalse(error.message.orEmpty().contains("secret-native-http-test-token"))
        assertEquals(1, stats("denied").getValue("status401"))
    }
    @Test fun rejectsFullBodiesInvalidEncodingLengthTruncationAndRedirects() = runTest(timeout = 30.seconds) {
        val r = Gdal4k.runtime()
        for (case in listOf("full", "encoded", "length", "truncated", "redirect")) {
            assertFailsWith<GdalException> { r.openDataset(url(case), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy(maxRetries = 2))) }
            assertEquals(1, stats(case).getValue("requests"), case)
        }
        assertTrue(r.versionInfo().isNotEmpty())
    }
    @Test fun identityAndSizeChangesPoisonInputAndLeaveRuntimeUsable() = runTest(timeout = 30.seconds) {
        val r = Gdal4k.runtime()
        for (case in listOf("identity", "size-change")) {
            r.openDataset(url(case), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy())).use { ds ->
                requireNotNull(ds.getRasterBand(1)).use { band ->
                    val target = ByteArray(256 * 256) { 85 }
                    assertFailsWith<GdalException> { band.readRaster(BandRasterIOOptions(1792, 1792, 256, 256), target) }
                    assertTrue(target.all { it == 85.toByte() }, "Failed HTTP read must preserve the caller's array")
                }
                val before = stats(case).getValue("requests")
                assertFails { readLast(ds) }
                assertEquals(before, stats(case).getValue("requests"))
            }
        }
        r.openDataset(url("ok", "-after-identity"), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy())).use { readLast(it) }
    }
    @Test fun strongEtagLastModifiedAndSizeOnlyPolicies() = runTest(timeout = 30.seconds) {
        val r = Gdal4k.runtime()
        assertFailsWith<GdalException> { r.openDataset(url("weak", "-strong"), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy(requireStrongETag = true))) }
        r.openDataset(url("weak"), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy())).use { readLast(it) }
        assertTrue(stats("weak").getValue("modifiedRange") > 0)
        r.openDataset(url("no-validator"), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy())).use { readLast(it) }
    }
    @Test fun trustedTlsAndUntrustedOrWrongHostnameRejection() = runTest(timeout = 30.seconds) {
        val r = Gdal4k.runtime()
        r.openDataset(url("ok", origin = "@HTTPS_BASE@"), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy())).use { readLast(it) }
        for (origin in listOf("@UNTRUSTED_BASE@", "@WRONG_NAME_BASE@"))
            assertFailsWith<GdalException> { r.openDataset(url("ok", origin = origin), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy(maxRetries = 2))) }
    }
    @Test fun requestDeadlineHasBoundedRetryAndNoFullDownloadFallback() = runTest(timeout = 30.seconds) {
        assertFailsWith<GdalException> {
            Gdal4k.runtime().openDataset(url("timeout"), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy(requestTimeoutMillis = 100, maxRetries = 1, retryDelayMillis = 0)))
        }
        assertEquals(2, stats("timeout").getValue("requests"))
    }
    @Test fun cancellationInterruptsHeadersBodyAndRetryBackoff() = runTest(timeout = 30.seconds) {
        val r = Gdal4k.runtime()
        for (case in listOf("slow", "slow-body", "backoff")) {
            withContext(Dispatchers.Default) {
                val task = async {
                    r.openDataset(url(case), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy(requestTimeoutMillis = 5000, maxRetries = 2))).use { readLast(it) }
                }
                delay(500)
                task.cancel()
                assertFailsWith<CancellationException> { task.await() }
            }
            val result = stats(case)
            assertTrue(result.getValue("requests") >= 1, "Must cancel actual in-flight HTTP work")
            if (case == "backoff") assertEquals(1, result.getValue("requests"))
            assertTrue(r.versionInfo().isNotEmpty())
        }
    }
    @Test fun updateAndUserinfoAreRejectedWithoutChangingLocalInputPolicy() = runTest(timeout = 30.seconds) {
        val r = Gdal4k.runtime()
        assertFailsWith<GdalException> { r.openDataset(url("ok", "-update"), DatasetOpenOptions(accessMode = DatasetAccessMode.Update, httpInputPolicy = HttpInputPolicy())) }
        assertFailsWith<GdalException> { r.openDataset(url("ok").replace("://", "://name:secret@"), DatasetOpenOptions(httpInputPolicy = HttpInputPolicy())) }
        assertTrue(r.versionInfo().isNotEmpty())
    }
}

package dev.gdal4k

import dev.gdal4k.internal.BundledGdalRuntime
import dev.gdal4k.internal.RuntimeEngine
import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import kotlin.test.*

/** Read the native sequence directly: start/end wrapper events alone would give only two.
 * Synchronous test transport avoids timing assertions and polling races on tiny fixtures.
 */
class GdalProgressNativeCallbacksTest {
    private class NativeCalls : GdalTransport {
        private val bridge = JniBridge()
        private val context: Long
        val callbacks = mutableMapOf<String, MutableList<Long>>()
        private val operations = setOf("sieveTo", "proximityTo", "fillNoData", "contour", "grid", "viewshed", "overlayTo")
        init { loadBundledLibrary(null); context = bridge.create(byteArrayOf(), byteArrayOf()); check(context != 0L) }
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            val name = Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject.getValue("method").jsonPrimitive.content.substringBefore('_')
            if (name in operations) assertNotNull(control, "$name must support cancellation without an observer")
            if (control == null) return bridge.execute(context, request.encodeToByteArray()).decodeToString()
            val token = bridge.controlCreate(); check(token != 0L)
            try {
                val response = bridge.executeControlled(context, request.encodeToByteArray(), token).decodeToString()
                val snapshot = bridge.controlSnapshot(token).decodeToString()
                if (name in operations) {
                    val sequence = Json.parseToJsonElement(snapshot).jsonObject.getValue("sequence").jsonPrimitive.long
                    assertTrue(sequence > 2, "$name emitted $sequence events; GDAL itself must call progress")
                    callbacks.getOrPut(name) { mutableListOf() }.add(sequence)
                }
                control.accept(snapshot)
                return response
            } finally { bridge.controlDestroy(token) }
        }
        override suspend fun close() { bridge.destroy(context) }
    }
    private suspend fun <T> runtime(block: suspend (GdalRuntime, NativeCalls) -> T): T {
        val calls = NativeCalls(); val engine = RuntimeEngine(calls)
        try { return block(BundledGdalRuntime(engine), calls) } finally { engine.close() }
    }
    private suspend fun <T> raster(r: GdalRuntime, block: suspend (Dataset) -> T): T = requireNotNull(r.getDriver("MEM")).use { driver ->
        requireNotNull(driver.create("", 16, 16, 1, GdalDataType.Byte)).use { ds ->
            ds.writeRaster(ByteArray(256) { (it % 17).toByte() })
            ds.setGeoTransform(doubleArrayOf(0.0, 1.0, 0.0, 16.0, 0.0, -1.0))
            r.createSpatialReference("EPSG:3857").use { ds.setSpatialRef(it) }; block(ds)
        }
    }
    @Test fun bandAnalysisAndContourExecuteNativeCallbacks(): Unit = runBlocking { runtime { r, calls ->
        raster(r) { source -> raster(r) { target ->
            requireNotNull(source.getRasterBand(1)).use { band -> requireNotNull(target.getRasterBand(1)).use { out ->
                assertEquals(0, band.sieveTo(out, 2)); assertEquals(0, band.proximityTo(out))
                band.setNoDataValue(0.0); assertEquals(0, band.fillNoData(maxSearchDistance = 2.0))
            } }
            r.openDataset(source.contour(interval = 5.0)).use { ds -> requireNotNull(ds.getLayer(0)).use { assertTrue(it.getFeatureCount(true) > 0) } }
            assertEquals(16, source.getRasterXSize())
        } }
        assertEquals(setOf("sieveTo", "proximityTo", "fillNoData", "contour"), calls.callbacks.keys)
    } }
    @Test fun gridAndViewshedExecuteNativeCallbacks(): Unit = runBlocking { runtime { r, calls ->
        r.openDataset(GdalFiles(GdalFile("points.geojson", """{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},"geometry":{"type":"Point","coordinates":[0,0,3]}}]}""".encodeToByteArray()))).use { source ->
            r.openDataset(source.grid("-a", "nearest", "-txe", "0", "2", "-tye", "0", "2", "-outsize", "2", "2")).use { assertEquals(2, it.getRasterXSize()) }
        }
        raster(r) { source -> r.openDataset(source.viewshed(ViewshedOptions(8.5, 8.5, curvatureCoefficient = 0.0))).use { assertEquals(16, it.getRasterYSize()) } }
        assertEquals(setOf("grid", "viewshed"), calls.callbacks.keys)
    } }
    @Test fun everyLayerOverlayExecutesNativeCallbacks(): Unit = runBlocking { runtime { r, calls ->
        requireNotNull(r.getDriver("MEM")).use { driver -> requireNotNull(driver.createVector("")).use { ds ->
            suspend fun layer(name: String, x: Int?): Layer {
                val layer = requireNotNull(ds.createLayer(name, geometryType = 0))
                if (x != null) requireNotNull(layer.getLayerDefn()).use { schema -> r.createFeature(schema).use { f ->
                    r.createGeometryFromWkt("POLYGON (($x 0,${x+2} 0,${x+2} 2,$x 2,$x 0))").use { f.setGeometry(it) }
                    assertEquals(0, layer.createFeature(f))
                } }
                return layer
            }
            layer("input", 0).use { input -> layer("method", 1).use { method ->
                for (op in LayerOverlayOperation.entries) layer("out-${op.name}", null).use { out -> assertEquals(0, input.overlayTo(method, out, op)) }
                assertEquals(1L, input.getFeatureCount(true)); assertEquals(1L, method.getFeatureCount(true))
            } }
        } }
        assertEquals(7, calls.callbacks.getValue("overlayTo").size)
    } }
}

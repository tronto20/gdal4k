package dev.gdal4k

import dev.gdal4k.runtime.*
import java.lang.reflect.Proxy
import kotlinx.coroutines.runBlocking
import kotlin.test.*

class UtilityArgumentsTest {
    private data class Call(val name: String, val values: List<Any?>)
    private val calls = mutableListOf<Call>()
    private val files = GdalFiles(GdalFile("result.tif", byteArrayOf()))
    private val dataset: Dataset = receiver(Dataset::class.java)
    private val runtime: GdalRuntime = receiver(GdalRuntime::class.java)

    private fun <T> receiver(type: Class<T>): T = type.cast(Proxy.newProxyInstance(
        type.classLoader, arrayOf(type),
    ) { _, method, args ->
        calls += Call(method.name, args!!.dropLast(1)) // suspend continuation is not an API argument
        when (method.name) {
            "buildVRT" -> dataset
            "info" -> "{}"
            "warpTo", "translateTo", "translateVectorTo", "rasterizeTo", "demProcessingTo", "tileIndexTo" -> Unit
            else -> files
        }
    })

    @Test fun positionalOptionsAndNamedOutputSettings() = runBlocking {
        assertSame(files, dataset.translate("-co", "COMPRESS=JXL", driver = "COG", name = "cog.tif"))
        dataset.translateTo("/out/cog.tif", "-co", "COMPRESS=JXL", driver = "COG")
        dataset.warp("EPSG:3857", "-r", "bilinear", driver = "COG", name = "warp.tif")
        dataset.warpTo("/out/warp.tif", "EPSG:3857", "-r", "bilinear", driver = "COG")
        dataset.translateVector("-where", "value > 1", driver = "GPKG", name = "out.gpkg")
        dataset.translateVectorTo("/out/out.gpkg", "-where", "value > 1", driver = "GPKG")
        dataset.rasterize("-burn", "1", driver = "COG", name = "burn.tif")
        dataset.rasterizeTo("/out/burn.tif", "-burn", "1", driver = "COG")
        dataset.demProcessing("slope", "-s", "1", driver = "COG", name = "slope.tif")
        dataset.demProcessingTo("/out/slope.tif", "slope", "-s", "1", driver = "COG")
        assertEquals("{}", dataset.info("-stats"))
        assertEquals(listOf(
            Call("translate", listOf("COG", "cog.tif", listOf("-co", "COMPRESS=JXL"))),
            Call("translateTo", listOf("/out/cog.tif", "COG", listOf("-co", "COMPRESS=JXL"))),
            Call("warp", listOf("EPSG:3857", "COG", "warp.tif", listOf("-r", "bilinear"))),
            Call("warpTo", listOf("/out/warp.tif", "EPSG:3857", "COG", listOf("-r", "bilinear"))),
            Call("translateVector", listOf("GPKG", "out.gpkg", listOf("-where", "value > 1"))),
            Call("translateVectorTo", listOf("/out/out.gpkg", "GPKG", listOf("-where", "value > 1"))),
            Call("rasterize", listOf("COG", "burn.tif", listOf("-burn", "1"))),
            Call("rasterizeTo", listOf("/out/burn.tif", "COG", listOf("-burn", "1"))),
            Call("demProcessing", listOf("slope", "COG", "slope.tif", listOf("-s", "1"))),
            Call("demProcessingTo", listOf("/out/slope.tif", "slope", "COG", listOf("-s", "1"))),
            Call("info", listOf(listOf("-stats"))),
        ), calls)
    }

    @Test fun multipleInputUtilitiesAndSpread() = runBlocking {
        val sources = listOf(dataset)
        val args = arrayOf("-r", "near")
        assertSame(dataset, runtime.buildVRT(sources, "-separate"))
        assertSame(files, runtime.warp(sources, "EPSG:4326", *args, driver = "COG", name = "all.tif"))
        runtime.warpTo(sources, "/out/all.tif", "EPSG:4326", *args, driver = "COG")
        assertEquals(listOf(
            Call("buildVRT", listOf(sources, listOf("-separate"))),
            Call("warp", listOf(sources, "EPSG:4326", "COG", "all.tif", args.asList())),
            Call("warpTo", listOf(sources, "/out/all.tif", "EPSG:4326", "COG", args.asList())),
        ), calls)
    }

    @Test fun emptyOptionsAndExistingLists() = runBlocking {
        dataset.info()
        dataset.translate()
        dataset.translateTo("out.tif")
        dataset.warp("EPSG:4326")
        dataset.warpTo("out.tif", "EPSG:4326")
        dataset.translateVector()
        dataset.translateVectorTo("out.geojson")
        dataset.rasterize()
        dataset.rasterizeTo("out.tif")
        dataset.demProcessing("slope")
        dataset.demProcessingTo("out.tif", "slope")
        runtime.buildVRT(listOf(dataset))
        runtime.warp(listOf(dataset), "EPSG:4326")
        runtime.warpTo(listOf(dataset), "out.tif", "EPSG:4326")
        assertEquals(14, calls.size)
        assertTrue(calls.all { it.values.last() == emptyList<String>() })

        val args = listOf("-co", "COMPRESS=JXL")
        dataset.translate(driver = "COG", arguments = args)
        assertEquals(Call("translate", listOf("COG", "output.tif", args)), calls.last())
        assertSame(args, calls.last().values.last())
        dataset.translate("COG", "old-positional-list.tif", args)
        assertEquals(Call("translate", listOf("COG", "old-positional-list.tif", args)), calls.last())
        runtime.warp(listOf(dataset), "EPSG:4326", arguments = args)
        assertSame(args, calls.last().values.last())
    }

    @Test fun namedVarargArrayPreservesOptions() = runBlocking {
        val args = arrayOf("-compute_edges", "-s", "1")
        dataset.demProcessing("slope", arguments = args)
        assertEquals(Call("demProcessing", listOf("slope", "GTiff", "terrain.tif", args.asList())), calls.single())
    }

    @Test fun tileIndexExplicitLocationsAndNamedSettings() = runBlocking {
        val paths = listOf("a.tif", "b.tif")
        val args = arrayOf("-t_srs", "EPSG:3857")
        assertSame(files, runtime.tileIndex(paths, *args, driver = "GeoJSON", name = "catalog.geojson", layerName = "catalog", locationField = "raster", absolutePaths = true))
        runtime.tileIndexTo(paths, "out.gpkg", *args)
        assertEquals(listOf(
            Call("tileIndex", listOf(paths, "GeoJSON", "catalog.geojson", "catalog", "raster", true, args.asList())),
            Call("tileIndexTo", listOf(paths, "out.gpkg", "GPKG", "tiles", "location", false, args.asList())),
        ), calls)
    }
}

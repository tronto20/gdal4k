package dev.gdal4k

import dev.gdal4k.GdalException
import dev.gdal4k.runtime.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class MDArrayResampledApiTest {
    // Explicit WKT node keys select the CRS authority; an empty string is not GDAL's null root key.
    private val samples = DoubleArray(16) { it + 1.0 }
    private val nearest = doubleArrayOf(6.0, 8.0, 14.0, 16.0)
    private val average = doubleArrayOf(3.5, 5.5, 11.5, 13.5)

    private suspend fun memory(block: suspend (Group) -> Unit) {
        requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
            requireNotNull(driver.createMultidimensional("")).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { block(it) }
            }
        }
    }

    private suspend fun axis(group: Group, name: String, coordinates: DoubleArray,
                             type: String, direction: String): Dimension {
        val dimension = requireNotNull(group.createDimension(name, coordinates.size.toLong(), type, direction))
        try {
            requireNotNull(group.createArray(name, listOf(dimension), GdalDataType.Float64)).use { array ->
                assertTrue(array.write(longArrayOf(0), longArrayOf(coordinates.size.toLong()), coordinates))
                assertTrue(dimension.setIndexingVariable(array))
            }
            return dimension
        } catch (failure: Throwable) {
            dimension.close()
            throw failure
        }
    }

    private suspend fun axes(group: Group, prefix: String, block: suspend (Dimension, Dimension) -> Unit) {
        axis(group, "${prefix}_y", doubleArrayOf(3.5, 2.5, 1.5, 0.5), "HORIZONTAL_Y", "NORTH").use { y ->
            axis(group, "${prefix}_x", doubleArrayOf(0.5, 1.5, 2.5, 3.5), "HORIZONTAL_X", "EAST").use { x ->
                block(y, x)
            }
        }
    }

    private suspend fun shape(array: MDArray): LongArray =
        array.getDimensions().map { dimension -> dimension.use { it.getSize() } }.toLongArray()

    private suspend fun outputAxes(array: MDArray, expectedY: DoubleArray, expectedX: DoubleArray) {
        val dimensions = array.getDimensions()
        try {
            assertEquals(listOf("dimY", "dimX"), dimensions.map { it.getName() })
            for ((dimension, expected) in dimensions.zip(listOf(expectedY, expectedX))) {
                assertEquals(expected.size.toLong(), dimension.getSize())
                requireNotNull(dimension.getIndexingVariable()).use { indexing ->
                    assertContentEquals(longArrayOf(expected.size.toLong()), shape(indexing))
                    assertContentEquals(expected, indexing.read(longArrayOf(0), longArrayOf(expected.size.toLong())))
                }
            }
        } finally {
            for (dimension in dimensions.asReversed()) dimension.close()
        }
    }

    @Test fun automaticAndExplicitShapesUseNativeInterpolationAndOwnedAxes() = runTest(timeout = 120.seconds) {
        memory { group -> axes(group, "source") { y, x ->
            requireNotNull(group.createArray("signal", listOf(y, x), GdalDataType.Float64)).use { source ->
                assertTrue(source.write(longArrayOf(0, 0), longArrayOf(4, 4), samples))
                source.getResampled(listOf(null, null)).use { automatic ->
                    assertContentEquals(longArrayOf(4, 4), shape(automatic))
                    assertEquals(MDDataType.Numeric(GdalDataType.Float64), automatic.getExtendedDataType())
                    assertContentEquals(samples, automatic.read(longArrayOf(0, 0), longArrayOf(4, 4)))
                    outputAxes(automatic, doubleArrayOf(3.5, 2.5, 1.5, 0.5), doubleArrayOf(0.5, 1.5, 2.5, 3.5))
                }
                val targetY = requireNotNull(group.createDimension("target_y", 2))
                val targetX = requireNotNull(group.createDimension("target_x", 2))
                val reduced = source.getResampled(listOf(targetY, targetX))
                try {
                    // The native view rebuilds its spatial dimensions and does not borrow these wrappers.
                    targetY.close(); targetX.close()
                    assertContentEquals(longArrayOf(2, 2), shape(reduced))
                    assertContentEquals(nearest, reduced.read(longArrayOf(0, 0), longArrayOf(2, 2)))
                    outputAxes(reduced, doubleArrayOf(3.0, 1.0), doubleArrayOf(1.0, 3.0))
                    assertFalse(reduced.write(longArrayOf(0, 0), longArrayOf(1, 1), doubleArrayOf(99.0)))
                    assertFalse(reduced.writeValues(longArrayOf(0, 0), longArrayOf(1, 1), listOf(MDValue.Numeric(99))))
                    assertContentEquals(samples, source.read(longArrayOf(0, 0), longArrayOf(4, 4)))
                } finally {
                    reduced.close(); targetX.close(); targetY.close()
                }
                requireNotNull(group.createDimension("automatic_x", 2)).use { target ->
                    source.getResampled(listOf(null, target)).use { view ->
                        assertContentEquals(longArrayOf(2, 2), shape(view))
                        assertContentEquals(nearest, view.read(longArrayOf(0, 0), longArrayOf(2, 2)))
                    }
                }
                axis(group, "indexed_y", doubleArrayOf(3.0, 1.0), "HORIZONTAL_Y", "NORTH").use { targetY2 ->
                    axis(group, "indexed_x", doubleArrayOf(1.0, 3.0), "HORIZONTAL_X", "EAST").use { targetX2 ->
                        source.getResampled(listOf(targetY2, targetX2), RasterResampling.Average).use { view ->
                            assertEquals(MDDataType.Numeric(GdalDataType.Float64), view.getExtendedDataType())
                            assertContentEquals(average, view.read(longArrayOf(0, 0), longArrayOf(2, 2)))
                            outputAxes(view, doubleArrayOf(3.0, 1.0), doubleArrayOf(1.0, 3.0))
                        }
                    }
                }
            }
        } }
    }

    @Test fun smallIntegerAndComplexNearestViewsPreserveTheirStoredTypes() = runTest(timeout = 120.seconds) {
        memory { group -> axes(group, "typed") { y, x ->
            for (type in listOf(GdalDataType.Int16, GdalDataType.CFloat64)) {
                val values = (1..16).map {
                    if (type == GdalDataType.CFloat64) MDValue.Numeric(it.toString(), (-it).toString())
                    else MDValue.Numeric(it)
                }
                requireNotNull(group.createArray("typed$type", listOf(y, x), type)).use { source ->
                    assertTrue(source.writeValues(longArrayOf(0, 0), longArrayOf(4, 4), values))
                    source.getResampled(listOf(null, null)).use { view ->
                        assertEquals(MDDataType.Numeric(type), view.getExtendedDataType())
                        assertEquals(values, view.readValues(longArrayOf(0, 0), longArrayOf(4, 4)))
                    }
                }
            }
        } }
    }

    @Test fun consecutiveNonspatialSlicesKeepTheirValuesAndDimensions() = runTest(timeout = 120.seconds) {
        memory { group -> axes(group, "cube") { y, x ->
            requireNotNull(group.createDimension("time", 3)).use { time ->
                requireNotNull(group.createArray("cube", listOf(time, y, x), GdalDataType.Float64)).use { source ->
                    val values = DoubleArray(48) { (it / 16) * 100.0 + it % 16 + 1.0 }
                    assertTrue(source.write(longArrayOf(0, 0, 0), longArrayOf(3, 4, 4), values))
                    source.getResampled(listOf(time, null, null)).use { view ->
                        assertContentEquals(longArrayOf(3, 4, 4), shape(view))
                        val dimensions = view.getDimensions()
                        try {
                            assertEquals(listOf("time", "dimY", "dimX"), dimensions.map { it.getName() })
                        } finally { for (dimension in dimensions.asReversed()) dimension.close() }
                        assertContentEquals(values, view.read(longArrayOf(0, 0, 0), longArrayOf(3, 4, 4)))
                        assertContentEquals(values.copyOfRange(16, 48), view.read(longArrayOf(1, 0, 0), longArrayOf(2, 4, 4)))
                        view.getView("[::2,:,:]").use { selected ->
                            assertContentEquals(values.copyOfRange(0, 16) + values.copyOfRange(32, 48),
                                selected.read(longArrayOf(0, 0, 0), longArrayOf(2, 4, 4)))
                        }
                        view.getView("[::-1,:,:]").use { reversed ->
                            assertContentEquals(values.copyOfRange(32, 48) + values.copyOfRange(16, 32) + values.copyOfRange(0, 16),
                                reversed.read(longArrayOf(0, 0, 0), longArrayOf(3, 4, 4)))
                        }
                        view.getView("[0,:,:]").use { selected ->
                            assertContentEquals(samples, selected.read(longArrayOf(0, 0), longArrayOf(4, 4)))
                        }
                        view.transpose(intArrayOf(1, 2, 0)).use { reordered ->
                            reordered.getView("[:,:,::-2]").use { selected ->
                                val expected = DoubleArray(32) { i ->
                                    val pixel = i / 2
                                    values[(if (i % 2 == 0) 2 else 0) * 16 + pixel]
                                }
                                assertContentEquals(expected, selected.read(longArrayOf(0, 0, 0), longArrayOf(4, 4, 2)))
                            }
                        }
                        assertContentEquals(values, view.read(longArrayOf(0, 0, 0), longArrayOf(3, 4, 4)))
                    }
                    requireNotNull(group.createDimension("different_time", 3)).use { changedName ->
                        assertFails { source.getResampled(listOf(changedName, null, null)) }
                    }
                    requireNotNull(group.createGroup("different_size")).use { other ->
                        requireNotNull(other.createDimension("time", 2)).use { changedSize ->
                            assertFails { source.getResampled(listOf(changedSize, null, null)) }
                        }
                    }
                    assertContentEquals(values, source.read(longArrayOf(0, 0, 0), longArrayOf(3, 4, 4)))
                    // A separately sliced source is another independent composition path.
                    source.getView("[::2,:,:]").use { selected ->
                        selected.getResampled(listOf(null, null, null)).use { view ->
                            assertContentEquals(values.copyOfRange(0, 16) + values.copyOfRange(32, 48),
                                view.read(longArrayOf(0, 0, 0), longArrayOf(2, 4, 4)))
                        }
                    }
                }
            }
        } }
    }

    @Test fun twoExtraAxesHonorSignedStepsAndResetInnerSlices() = runTest(timeout = 120.seconds) {
        memory { group -> axes(group, "rank4") { y, x ->
            requireNotNull(group.createDimension("outer", 3)).use { outer ->
                requireNotNull(group.createDimension("inner", 3)).use { inner ->
                    requireNotNull(group.createArray("rank4", listOf(outer, inner, y, x), GdalDataType.Float64)).use { source ->
                        val values = DoubleArray(144) { i -> 1000.0 * (i / 48) + 100.0 * ((i / 16) % 3) + i % 16 + 1.0 }
                        assertTrue(source.write(longArrayOf(0, 0, 0, 0), longArrayOf(3, 3, 4, 4), values))
                        source.getResampled(listOf(null, null, null, null)).use { view ->
                            view.getView("[::2,::-1,:,:]").use { selected ->
                                val expected = DoubleArray(96) { i ->
                                    val a = (i / 48) * 2
                                    val b = 2 - (i / 16) % 3
                                    1000.0 * a + 100.0 * b + i % 16 + 1.0
                                }
                                assertContentEquals(expected, selected.read(longArrayOf(0, 0, 0, 0), longArrayOf(2, 3, 4, 4)))
                                selected.getView("[::-1,::2,:,:]").use { chained ->
                                    val chainedExpected = DoubleArray(64) { i ->
                                        val a = if (i / 32 == 0) 2 else 0
                                        val b = if ((i / 16) % 2 == 0) 2 else 0
                                        1000.0 * a + 100.0 * b + i % 16 + 1.0
                                    }
                                    assertContentEquals(chainedExpected, chained.read(longArrayOf(0, 0, 0, 0), longArrayOf(2, 2, 4, 4)))
                                }
                            }
                            assertContentEquals(values, view.read(longArrayOf(0, 0, 0, 0), longArrayOf(3, 3, 4, 4)))
                        }
                    }
                }
            }
        } }
    }

    @Test fun invalidNativeShapesCoordinatesAndTypesLeaveSourceUsable() = runTest(timeout = 120.seconds) {
        memory { group -> axes(group, "valid") { y, x ->
            requireNotNull(group.createArray("signal", listOf(y, x), GdalDataType.Float64)).use { source ->
                assertTrue(source.write(longArrayOf(0, 0), longArrayOf(4, 4), samples))
                for (dimensions in listOf(emptyList(), listOf(null), listOf(null, null, null))) {
                    assertFails { source.getResampled(dimensions) }
                }
                assertFails { source.getResampled(listOf(null, null), RasterResampling.Gauss) }
                assertFails { source.getResampled(listOf(null, null), options = listOf("BAD\u0000OPTION")) }
                requireNotNull(group.createDimension("huge_metadata_only", 2147483648L)).use { huge ->
                    assertFails { source.getResampled(listOf(null, huge)) }
                }
                axis(group, "irregular_y", doubleArrayOf(3.5, 2.5, 0.5), "HORIZONTAL_Y", "NORTH").use { irregular ->
                    assertFails { source.getResampled(listOf(irregular, x)) }
                }
                requireNotNull(group.createDimension("unindexed_x", 2)).use { unindexed ->
                    assertFails { source.getResampled(listOf(y, unindexed)) }
                }
                assertContentEquals(samples, source.read(longArrayOf(0, 0), longArrayOf(4, 4)))
                source.getResampled(listOf(null, null)).use {
                    assertContentEquals(samples, it.read(longArrayOf(0, 0), longArrayOf(4, 4)))
                }
            }
            for ((name, type) in listOf("text" to MDDataType.Text(), "compound" to
                MDDataType.Compound("record", listOf(MDComponent("value", MDDataType.Numeric(GdalDataType.Int16)))))) {
                requireNotNull(group.createArray(name, listOf(y, x), type)).use { source ->
                    assertFailsWith<GdalException> { source.getResampled(listOf(null, null)) }
                    assertEquals(name, source.getName())
                }
            }
            requireNotNull(group.createArray("scalar", emptyList(), GdalDataType.Float64)).use { scalar ->
                assertFails { scalar.getResampled(emptyList()) }
            }
            requireNotNull(group.createArray("line", listOf(x), GdalDataType.Float64)).use { line ->
                assertFails { line.getResampled(listOf(null)) }
            }
            requireNotNull(group.createDimension("unlocated_y", 4)).use { plainY ->
                requireNotNull(group.createDimension("unlocated_x", 4)).use { plainX ->
                    requireNotNull(group.createArray("unlocated", listOf(plainY, plainX), GdalDataType.Float64)).use { unlocated ->
                        assertTrue(unlocated.write(longArrayOf(0, 0), longArrayOf(4, 4), samples))
                        assertFailsWith<GdalException> { unlocated.getResampled(listOf(null, null)) }
                        assertContentEquals(samples, unlocated.read(longArrayOf(0, 0), longArrayOf(4, 4)))
                    }
                }
            }
        } }
    }

    @Test fun viewClosureIsIndependentAndSourceClosureInvalidatesDescendants() = runTest(timeout = 120.seconds) {
        memory { group -> axes(group, "owners") { y, x ->
            requireNotNull(group.createArray("signal", listOf(y, x), GdalDataType.Float64)).use { source ->
                assertTrue(source.write(longArrayOf(0, 0), longArrayOf(4, 4), samples))
                val view = source.getResampled(listOf(null, null))
                val sibling = source.getResampled(listOf(null, null))
                val descendant = sibling.getView("[:,:]")
                try {
                    view.close(); view.close()
                    assertContentEquals(samples, source.read(longArrayOf(0, 0), longArrayOf(4, 4)))
                    assertContentEquals(samples, sibling.read(longArrayOf(0, 0), longArrayOf(4, 4)))
                    source.close()
                    assertFailsWith<IllegalStateException> { sibling.getName() }
                    assertFailsWith<IllegalStateException> { descendant.read(longArrayOf(0, 0), longArrayOf(4, 4)) }
                } finally { descendant.close(); sibling.close(); view.close() }
            }
        } }
    }

    @Test fun signedThreeDimensionalCrsMappingsMatchIndependentClassicWarps() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        val path = "/vsimem/$datasetOutputPrefix-md-resampled-signed.vrt"
        val cases = listOf(Triple("negative_x", "2,-3", "2,-1"), Triple("negative_y", "-2,3", "-2,1"))
        val values = DoubleArray(32) { i -> 100.0 * (i / 16) + i % 16 + 1.0 }
        val allBands = RasterIOOptions(bands = intArrayOf(1, 2))
        val xml = buildString {
            append("""<VRTDataset><Group name="/"><Dimension name="time" size="2"/><Dimension name="y" size="4" indexingVariable="y"/><Dimension name="x" size="4" indexingVariable="x"/>""")
            append("""<Array name="y"><DataType>Float64</DataType><DimensionRef ref="/y"/><RegularlySpacedValues start="3.5" increment="-1"/></Array>""")
            append("""<Array name="x"><DataType>Float64</DataType><DimensionRef ref="/x"/><RegularlySpacedValues start="0.5" increment="1"/></Array>""")
            for ((name, mapping, rasterMapping) in cases) {
                append("""<Array name="$name"><DataType>Float64</DataType><DimensionRef ref="/time"/><DimensionRef ref="/y"/><DimensionRef ref="/x"/><SRS dataAxisToSRSAxisMapping="$mapping">EPSG:4326</SRS><InlineValues>${values.joinToString(" ")}</InlineValues></Array>""")
                // This metadata holder supplies the independent classic raster's intended 2D mapping.
                append("""<Array name="${name}_raster_crs"><DataType>Float64</DataType><DimensionRef ref="/y"/><DimensionRef ref="/x"/><SRS dataAxisToSRSAxisMapping="$rasterMapping">EPSG:4326</SRS><InlineValues>${samples.joinToString(" ")}</InlineValues></Array>""")
            }
            append("</Group></VRTDataset>")
        }
        val inherited = mutableListOf<Pair<SpatialReference, IntArray>>()
        val projected = mutableListOf<SpatialReference>()
        runtime.writeMemoryFile(path, xml.encodeToByteArray())
        try {
            runtime.openDataset(path, DatasetOpenOptions(kind = DatasetKind.Multidimensional)).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { group ->
                    for ((name, mapping, rasterMapping) in cases) {
                        val expectedMapping = mapping.split(',').map { it.toInt() }.toIntArray()
                        val expectedRasterMapping = rasterMapping.split(',').map { it.toInt() }.toIntArray()
                        requireNotNull(group.openArray(name)).use { source ->
                            assertContentEquals(values, source.read(longArrayOf(0, 0, 0), longArrayOf(2, 4, 4)))
                            requireNotNull(source.getSpatialRef()).use { crs ->
                                assertContentEquals(expectedMapping, crs.getDataAxisToSRSAxisMapping())
                            }
                            source.getResampled(listOf(null, null, null)).use { view ->
                                // Target-null retains the full source mapping; no identity-grid assumption is made.
                                val copy = requireNotNull(view.getSpatialRef())
                                inherited += copy to expectedMapping
                                assertContentEquals(expectedMapping, copy.getDataAxisToSRSAxisMapping())
                            }
                            requireNotNull(runtime.getDriver("MEM")).use { driver ->
                                requireNotNull(driver.create("", 4, 4, 2, GdalDataType.Float64)).use { raster ->
                                    assertEquals(0, raster.setGeoTransform(doubleArrayOf(0.0, 1.0, 0.0, 4.0, 0.0, -1.0)))
                                    requireNotNull(group.openArray("${name}_raster_crs")).use { holder ->
                                        requireNotNull(holder.getSpatialRef()).use { crs ->
                                            assertContentEquals(expectedRasterMapping, crs.getDataAxisToSRSAxisMapping())
                                            assertEquals(0, raster.setSpatialRef(crs))
                                        }
                                    }
                                    requireNotNull(raster.getSpatialRef()).use { crs ->
                                        assertContentEquals(expectedRasterMapping, crs.getDataAxisToSRSAxisMapping())
                                    }
                                    // Use buffered Dataset RasterIO; readRaster() without a buffer reads band 1.
                                    assertEquals(0, raster.writeRaster(values, allBands))
                                    val readBack = DoubleArray(values.size)
                                    assertEquals(0, raster.readRaster(readBack, allBands))
                                    assertContentEquals(values, readBack)
                                    val files = raster.warp("EPSG:3857", name = "$name-resampled-reference.tif", arguments = listOf("-r", "near"))
                                    runtime.openDataset(files).use { reference ->
                                        assertEquals(2, reference.getRasterCount())
                                        val height = reference.getRasterYSize()
                                        val width = reference.getRasterXSize()
                                        val count = longArrayOf(2, height.toLong(), width.toLong())
                                        val geotransform = requireNotNull(reference.getGeoTransform())
                                        assertEquals(0.0, geotransform[2]); assertEquals(0.0, geotransform[4])
                                        val target = runtime.createSpatialReference("EPSG:3857")
                                        val targetMapping = target.getDataAxisToSRSAxisMapping()
                                        val view = try { source.getResampled(listOf(null, null, null), targetSRS = target) }
                                            finally { target.close() }
                                        view.use { result ->
                                            assertContentEquals(count, shape(result))
                                            val expected = DoubleArray(2 * height * width)
                                            assertEquals(0, reference.readRaster(expected, allBands))
                                            assertContentEquals(expected, result.read(longArrayOf(0, 0, 0), count))
                                            val dimensions = result.getDimensions()
                                            try {
                                                assertEquals(listOf("time", "dimY", "dimX"), dimensions.map { it.getName() })
                                                for ((dimension, expected) in listOf(
                                                    dimensions[1] to DoubleArray(height) { geotransform[3] + geotransform[5] * (it + 0.5) },
                                                    dimensions[2] to DoubleArray(width) { geotransform[0] + geotransform[1] * (it + 0.5) })) {
                                                    requireNotNull(dimension.getIndexingVariable()).use { axis ->
                                                        val actual = axis.read(longArrayOf(0), longArrayOf(expected.size.toLong()))
                                                        assertEquals(expected.size, actual.size)
                                                        for (i in expected.indices) assertEquals(expected[i], actual[i], 1e-7)
                                                    }
                                                }
                                            } finally { for (dimension in dimensions.asReversed()) dimension.close() }
                                            val copy = requireNotNull(result.getSpatialRef())
                                            projected += copy
                                            assertContentEquals(targetMapping, copy.getDataAxisToSRSAxisMapping())
                                        }
                                    }
                                }
                            }
                            requireNotNull(source.getSpatialRef()).use { crs ->
                                assertContentEquals(expectedMapping, crs.getDataAxisToSRSAxisMapping())
                            }
                            assertContentEquals(values, source.read(longArrayOf(0, 0, 0), longArrayOf(2, 4, 4)))
                        }
                    }
                }
            }
            // Both inherited and explicit-target copies survive every source, ancestor and view.
            for ((crs, mapping) in inherited) {
                assertTrue(crs.isGeographic()); assertContentEquals(mapping, crs.getDataAxisToSRSAxisMapping())
            }
            for (crs in projected) { assertTrue(crs.isProjected()); assertEquals("3857", crs.getAuthorityCode("PROJCS")) }
        } finally {
            for (crs in projected.asReversed()) crs.close()
            for ((crs, _) in inherited.asReversed()) crs.close()
            runtime.deleteMemoryFile(path)
        }
    }

    @Test fun explicitTargetCrsIsCopiedAndMatchesTheClassicWarp() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        var detached: SpatialReference? = null
        try {
            requireNotNull(runtime.getDriver("MEM")).use { driver ->
                requireNotNull(driver.create("", 4, 4, 1, GdalDataType.Float64)).use { raster ->
                    assertEquals(0, raster.setGeoTransform(doubleArrayOf(0.0, 1.0, 0.0, 4.0, 0.0, -1.0)))
                    runtime.createSpatialReference("EPSG:4326").use { srs ->
                        srs.setAxisMappingStrategy(0); assertEquals(0, raster.setSpatialRef(srs))
                    }
                    assertEquals(0, raster.writeRaster(samples))
                    val persisted = raster.translate(name = "resampled-source.tif", arguments = emptyList())
                    runtime.openDataset(persisted).use { fileBacked ->
                        assertEquals(1, fileBacked.getRasterCount())
                        assertEquals(4, fileBacked.getRasterXSize()); assertEquals(4, fileBacked.getRasterYSize())
                        assertContentEquals(doubleArrayOf(0.0, 1.0, 0.0, 4.0, 0.0, -1.0), fileBacked.getGeoTransform())
                        requireNotNull(fileBacked.getRasterBand(1)).use { assertEquals(GdalDataType.Float64, it.getRasterDataType()) }
                        assertContentEquals(samples, fileBacked.readRaster(1))
                        requireNotNull(fileBacked.getSpatialRef()).use {
                            assertTrue(it.isGeographic()); assertEquals("4326", it.getAuthorityCode("GEOGCS"))
                        }
                        val expected = fileBacked.warp("EPSG:3857", name = "resampled-reference.tif", arguments = listOf("-r", "near"))
                        runtime.openDataset(expected).use { reference ->
                            fileBacked.multiDimTranslate("-array", "band=1").use { multidimensional ->
                                requireNotNull(multidimensional.getRootGroup()).use { group ->
                                    requireNotNull(group.openArray("Band1")).use { source ->
                                        assertContentEquals(longArrayOf(4, 4), shape(source))
                                        assertEquals(MDDataType.Numeric(GdalDataType.Float64), source.getExtendedDataType())
                                        assertContentEquals(samples, source.read(longArrayOf(0, 0), longArrayOf(4, 4)))
                                        requireNotNull(source.getSpatialRef()).use {
                                            assertTrue(it.isGeographic()); assertEquals("4326", it.getAuthorityCode("GEOGCS"))
                                        }
                                        val target = runtime.createSpatialReference("EPSG:3857")
                                        val mapping = target.getDataAxisToSRSAxisMapping()
                                        val view = try { source.getResampled(listOf(null, null), targetSRS = target) }
                                            finally { target.close() }
                                        view.use {
                                            val sizes = longArrayOf(reference.getRasterYSize().toLong(), reference.getRasterXSize().toLong())
                                            assertContentEquals(sizes, shape(it))
                                            assertContentEquals(reference.readRaster(1), it.read(longArrayOf(0, 0), sizes))
                                            detached = requireNotNull(it.getSpatialRef())
                                            assertContentEquals(mapping, requireNotNull(detached).getDataAxisToSRSAxisMapping())
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            requireNotNull(detached).let { crs ->
                assertTrue(crs.isProjected()); assertEquals("3857", crs.getAuthorityCode("PROJCS"))
            }
        } finally { detached?.close() }
    }
}

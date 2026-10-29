package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class MDArrayMetadataApiTest {
    @Test fun memoryMetadataDistinguishesUnsetFromExplicitDefaultsWithoutChangingSamples() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        requireNotNull(runtime.getDriver("MEM")).use { driver ->
            requireNotNull(driver.createMultidimensional("")).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { group ->
                    requireNotNull(group.createArray("scalar", emptyList(), GdalDataType.Float64)).use { scalar ->
                        assertEquals(emptyList(), scalar.getBlockSize())
                        assertEquals(emptyList(), scalar.getStructuralInfo())
                        assertNull(scalar.getSpatialRef())
                        assertNull(scalar.getScaleEx()); assertNull(scalar.getOffsetEx())
                        assertTrue(scalar.setScale(1.0)); assertTrue(scalar.setOffset(0.0))
                        // MEM retains Unknown when the existing untyped setters are used.
                        assertEquals(MDNumericMetadata(1.0, GdalDataType.Unknown), scalar.getScaleEx())
                        assertEquals(MDNumericMetadata(0.0, GdalDataType.Unknown), scalar.getOffsetEx())
                    }
                    requireNotNull(group.createDimension("n", 2)).use { dimension ->
                        requireNotNull(group.createArray("values", listOf(dimension), GdalDataType.Float64)).use { array ->
                            val samples = doubleArrayOf(7.5, -2.0)
                            assertTrue(array.write(longArrayOf(0), longArrayOf(2), samples))
                            assertEquals(listOf(0uL), array.getBlockSize())
                            assertNull(array.getScale()); assertNull(array.getOffset())
                            assertNull(array.getScaleEx()); assertNull(array.getOffsetEx())
                            assertTrue(array.setScale(0.0)); assertTrue(array.setOffset(1.0))
                            assertEquals(MDNumericMetadata(0.0, GdalDataType.Unknown), array.getScaleEx())
                            assertEquals(MDNumericMetadata(1.0, GdalDataType.Unknown), array.getOffsetEx())
                            assertEquals(0.0, array.getScale()); assertEquals(1.0, array.getOffset())
                            assertContentEquals(samples, array.read(longArrayOf(0), longArrayOf(2)))
                        }
                    }
                }
            }
        }
    }

    @Test fun vrtMetadataCopiesOutliveOwnersAndPreserveSignedAxesAndUnsignedBlocks() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        val path = "/vsimem/$datasetOutputPrefix-md-metadata.vrt"
        val xml = """<VRTDataset><Group name="/">
            <Dimension name="y" size="2"/><Dimension name="x" size="3"/>
            <Array name="located"><DataType>Float64</DataType><DimensionRef ref="/y"/><DimensionRef ref="/x"/>
            <SRS dataAxisToSRSAxisMapping="-2,1">EPSG:4326</SRS><BlockSize>1,2</BlockSize><Scale>0</Scale><Offset>1</Offset></Array>
            <Array name="wide"><DataType>Byte</DataType><DimensionRef ref="/y"/><DimensionRef ref="/x"/>
            <BlockSize>9007199254740993,18446744073709551615</BlockSize></Array>
            </Group></VRTDataset>"""
        runtime.writeMemoryFile(path, xml.encodeToByteArray())
        var first: SpatialReference? = null
        var second: SpatialReference? = null
        try {
            runtime.openDataset(path, DatasetOpenOptions(kind = DatasetKind.Multidimensional)).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { group ->
                    requireNotNull(group.openArray("located")).use { array ->
                        assertEquals(listOf(1uL, 2uL), array.getBlockSize())
                        assertEquals(emptyList(), array.getStructuralInfo())
                        assertEquals(MDNumericMetadata(0.0, GdalDataType.Unknown), array.getScaleEx())
                        assertEquals(MDNumericMetadata(1.0, GdalDataType.Unknown), array.getOffsetEx())
                        first = requireNotNull(array.getSpatialRef())
                        second = requireNotNull(array.getSpatialRef())
                        assertContentEquals(intArrayOf(-2, 1), requireNotNull(first).getDataAxisToSRSAxisMapping())
                        requireNotNull(first).setAxisMappingStrategy(0)
                        assertContentEquals(intArrayOf(-2, 1), requireNotNull(second).getDataAxisToSRSAxisMapping())
                    }
                    requireNotNull(group.openArray("wide")).use { array ->
                        assertNull(array.getSpatialRef())
                        assertEquals(listOf(9_007_199_254_740_993uL, ULong.MAX_VALUE), array.getBlockSize())
                        assertNull(array.getScaleEx()); assertNull(array.getOffsetEx())
                    }
                }
            }
            // Both CRS copies must remain usable after the array, group and dataset close.
            assertTrue(requireNotNull(first).isGeographic())
            requireNotNull(first).close()
            assertFails { requireNotNull(first).getAxesCount() }
            assertTrue(requireNotNull(second).isGeographic())
            assertTrue(requireNotNull(second).exportToWkt().contains("WGS 84"))
            val mapping = requireNotNull(second).getDataAxisToSRSAxisMapping()
            mapping[0] = 1
            assertContentEquals(intArrayOf(-2, 1), requireNotNull(second).getDataAxisToSRSAxisMapping())
        } finally {
            first?.close(); second?.close()
            runtime.deleteMemoryFile(path)
        }
    }
}

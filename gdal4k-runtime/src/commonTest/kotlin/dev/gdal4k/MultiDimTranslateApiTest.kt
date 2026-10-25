package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class MultiDimTranslateApiTest {
    private suspend fun r() = Gdal4k.runtime()
    private suspend fun shape(array: MDArray): LongArray = array.getDimensions().map { d -> d.use { it.getSize() } }.toLongArray()
    private suspend fun values(array: MDArray): List<MDValue> {
        val count = shape(array)
        return array.readValues(LongArray(count.size), count)
    }
    private suspend fun text(group: Group, name: String, value: String) {
        requireNotNull(group.createAttribute(name, longArrayOf(), MDDataType.Text())).use {
            assertTrue(it.writeValues(listOf(MDValue.Text(value))))
        }
    }
    private suspend fun <T> fixture(xml: String, block: suspend (Dataset) -> T): T {
        val path = "/vsimem/multidim-conversion/input.vrt"
        r().writeMemoryFile(path, xml.encodeToByteArray())
        try { return r().openDataset(path, DatasetOpenOptions(kind = DatasetKind.Multidimensional)).use { block(it) } }
        finally { r().deleteMemoryFile("/vsimem/multidim-conversion") }
    }
    private fun cube(coordinates: Boolean = true, conventions: Boolean = true): String = buildString {
        append("<VRTDataset><Group name=\"/\">")
        for ((name, size) in listOf("time" to 3, "y" to 4, "x" to 5)) {
            append("<Dimension name=\"$name\" size=\"$size\"")
            if (coordinates) append(" indexingVariable=\"$name\"")
            append("/>")
            if (coordinates) append("<Array name=\"$name\"><DataType>Float64</DataType><DimensionRef ref=\"/$name\"/><RegularlySpacedValues start=\"${if (name == "time") 10 else 0}\" increment=\"${if (name == "time") 10 else 1}\"/></Array>")
        }
        if (conventions) append("<Attribute name=\"Conventions\"><DataType>String</DataType><Value>CF-1.8</Value></Attribute>")
        append("<Array name=\"cube\"><DataType>Float64</DataType><DimensionRef ref=\"/time\"/><DimensionRef ref=\"/y\"/><DimensionRef ref=\"/x\"/><InlineValues>")
        append((0 until 3).flatMap { t -> (0 until 4).flatMap { y -> (0 until 5).map { x -> t * 100 + y * 10 + x + 0.25 } } }.joinToString(" "))
        append("</InlineValues></Array><Group name=\"science\"><Dimension name=\"n\" size=\"2\"/><Attribute name=\"label\"><DataType>String</DataType><Value>observations</Value></Attribute><Array name=\"small\"><DataType>Int16</DataType><DimensionRef ref=\"n\"/><InlineValues>7 11</InlineValues></Array></Group></Group></VRTDataset>")
    }
    private fun plain(): String = """<VRTDataset><Group name="/"><Dimension name="y" size="4"/><Dimension name="x" size="5"/><Array name="values"><DataType>Float64</DataType><DimensionRef ref="/y"/><DimensionRef ref="/x"/><InlineValues>${(0 until 20).joinToString(" ") { (it + 0.25).toString() }}</InlineValues></Array></Group></VRTDataset>"""

    @Test fun fullMemoryTreePreservesExactTypesMetadataAndIndependentOwnership() = runTest(timeout = 120.seconds) {
        val source = requireNotNull(requireNotNull(r().getDriver("MEM")).use { it.createMultidimensional("") })
        val signed = listOf(Long.MIN_VALUE.toString(), "9007199254740993", Long.MAX_VALUE.toString()).map { MDValue.Numeric(it) }
        val unsigned = listOf("0", "9007199254740993", "18446744073709551615").map { MDValue.Numeric(it) }
        val complex = listOf(MDValue.Numeric("1.25", "-3.5"), MDValue.Numeric("-2", "4"), MDValue.Numeric("9", "0"))
        val strings = listOf(MDValue.Text(null), MDValue.Text(""), MDValue.Text("영상 🛰"))
        val recordType = MDDataType.Compound("observation", listOf(MDComponent("id", MDDataType.Numeric(GdalDataType.UInt64)), MDComponent("label", MDDataType.Text())))
        val records = unsigned.mapIndexed { i, id -> MDValue.Record(listOf(id, strings[i])) }
        val converted: Dataset
        try {
            requireNotNull(source.getRootGroup()).use { root ->
                text(root, "Conventions", "CF-1.8")
                requireNotNull(root.createAttribute("exact_ids", longArrayOf(2), MDDataType.Numeric(GdalDataType.UInt64))).use {
                    assertTrue(it.writeValues(unsigned.takeLast(2)))
                }
                val dimension = requireNotNull(root.createDimension("n", 3, type = "HORIZONTAL_X", direction = "EAST"))
                dimension.use { dim ->
                    assertNull(dim.getIndexingVariable())
                    requireNotNull(root.createArray("n", listOf(dim), GdalDataType.Float64)).use { axis ->
                        assertTrue(axis.write(longArrayOf(0), longArrayOf(3), doubleArrayOf(10.0, 20.0, 30.0)))
                        assertTrue(axis.setUnit("metre")); assertTrue(dim.setIndexingVariable(axis))
                    }
                    requireNotNull(root.createGroup("science")).use { group ->
                        text(group, "title", "Independent multidimensional copy")
                        requireNotNull(group.createGroup("nested")).use { nested ->
                            for ((name, type, input) in listOf(
                                Triple("signed", MDDataType.Numeric(GdalDataType.Int64), signed),
                                Triple("unsigned", MDDataType.Numeric(GdalDataType.UInt64), unsigned),
                                Triple("complex", MDDataType.Numeric(GdalDataType.CFloat64), complex),
                                Triple("strings", MDDataType.Text(), strings),
                                Triple("records", recordType, records),
                            )) requireNotNull(nested.createArray(name, listOf(dim), type)).use { array ->
                                assertTrue(array.writeValues(longArrayOf(0), longArrayOf(3), input))
                                textAttribute(array, "kind", name)
                                requireNotNull(array.createAttribute("record", longArrayOf(), recordType)).use { assertTrue(it.writeValues(listOf(records[2]))) }
                                if (name == "signed") {
                                    assertNull(array.getNoDataValue()); assertTrue(array.setNoDataValue(signed[0]))
                                    assertTrue(array.setUnit("metre")); assertNull(array.getScale()); assertNull(array.getOffset())
                                    assertFails { array.setUnit("metre\u0000truncated") }
                                    assertEquals("metre", array.getUnit())
                                    assertTrue(array.setScale(0.25)); assertTrue(array.setOffset(-4.0))
                                }
                                if (name == "unsigned") assertTrue(array.setNoDataValue(unsigned[2]))
                                if (name == "complex") assertTrue(array.setNoDataValue(complex[0]))
                                if (name == "strings") assertTrue(array.setNoDataValue(MDValue.Text(null)))
                                if (name == "records") assertTrue(array.setNoDataValue(records[2]))
                            }
                        }
                    }
                }
            }
            addJsonTree(source)
            converted = source.multiDimTranslate()
        } finally { source.close() }
        assertFails { source.getRootGroup() }
        converted.use { result ->
            assertJsonTree(result)
            requireNotNull(result.getRootGroup()).use { root ->
            requireNotNull(root.getAttribute("Conventions")).use { assertEquals(listOf(MDValue.Text("CF-1.8")), it.readValues()) }
            requireNotNull(root.getAttribute("exact_ids")).use { assertEquals(unsigned.takeLast(2), it.readValues()) }
            requireNotNull(root.openGroup("science")).use { group ->
                requireNotNull(group.getAttribute("title")).use { assertEquals(listOf(MDValue.Text("Independent multidimensional copy")), it.readValues()) }
                requireNotNull(group.openGroup("nested")).use { nested ->
                    for ((name, expected) in listOf("signed" to signed, "unsigned" to unsigned, "complex" to complex, "strings" to strings, "records" to records)) {
                        requireNotNull(nested.openArray(name)).use { array ->
                            assertContentEquals(longArrayOf(3), shape(array)); assertEquals(expected, values(array))
                            requireNotNull(array.getAttribute("kind")).use { assertEquals(listOf(MDValue.Text(name)), it.readValues()) }
                            requireNotNull(array.getAttribute("record")).use { assertEquals(listOf(records[2]), it.readValues()) }
                            if (name == "unsigned") assertEquals(unsigned[2], array.getNoDataValue())
                            if (name == "complex") assertEquals(complex[0], array.getNoDataValue())
                            if (name == "strings") assertEquals(MDValue.Text(null), array.getNoDataValue())
                            if (name == "records") assertEquals(records[2], array.getNoDataValue())
                            if (name == "signed") {
                                assertEquals(signed[0], array.getNoDataValue()); assertEquals("metre", array.getUnit())
                                assertEquals(0.25, array.getScale()); assertEquals(-4.0, array.getOffset())
                                array.getDimensions().single().use { dim ->
                                    assertEquals("HORIZONTAL_X", dim.getType()); assertEquals("EAST", dim.getDirection())
                                    requireNotNull(dim.getIndexingVariable()).use { assertEquals(listOf(MDValue.Numeric(10), MDValue.Numeric(20), MDValue.Numeric(30)), values(it)) }
                                }
                                assertTrue(array.setNoDataValue(null)); assertNull(array.getNoDataValue())
                                assertEquals(signed, values(array)) // Metadata does not scale, offset or replace raw values.
                            }
                        }
                    }
                }
            }
        } }
    }
    private class JsonTreeFixture {
        val type = MDDataType.JsonText()
        val values = listOf(MDValue.Text(null), MDValue.Text("null"),
            MDValue.Text(" \n" + """{"id":9007199254740993,"text":"영상 🛰","escaped":"\\ \n"}""" + "\t "))
        val recordType = MDDataType.Compound("json_observation", listOf(MDComponent("id", MDDataType.Numeric(GdalDataType.UInt64)),
            MDComponent("payload", MDDataType.Compound("json_payload", listOf(MDComponent("document", type), MDComponent("label", MDDataType.Text()))))))
        private val ids = listOf("0", "9007199254740993", "18446744073709551615").map { MDValue.Numeric(it) }
        private val labels = listOf(MDValue.Text(null), MDValue.Text(""), MDValue.Text("영상 🛰"))
        val records = ids.mapIndexed { i, id -> MDValue.Record(listOf(id, MDValue.Record(listOf(values[i], labels[i])))) }
        val plainRecordType = MDDataType.Compound("observation", listOf(MDComponent("id", MDDataType.Numeric(GdalDataType.UInt64)), MDComponent("label", MDDataType.Text())))
        val plainRecord = MDValue.Record(listOf(ids[2], labels[2]))
        val arrays: List<Triple<String, MDDataType, List<MDValue>>> = listOf(Triple("json", type, values), Triple("json_records", recordType, records))
        val arrayNames = listOf("signed", "unsigned", "complex", "strings", "records", "json", "json_records")
    }

    // Keep the coroutine for the existing whole-tree scenario below the JVM method-size limit.
    private suspend fun addJsonTree(source: Dataset) {
        val fixture = JsonTreeFixture()
        requireNotNull(source.getRootGroup()).use { root ->
            requireNotNull(root.createAttribute("json_config", longArrayOf(3), fixture.type)).use {
                assertEquals(fixture.type, it.getDataType()); assertTrue(it.writeValues(fixture.values)); assertEquals(fixture.values, it.readValues())
            }
            requireNotNull(root.openArray("n")).use { axis -> axis.getDimensions().single().use { dimension ->
                requireNotNull(root.openGroup("science")).use { group ->
                    requireNotNull(group.createAttribute("json_record", longArrayOf(), fixture.recordType)).use {
                        assertEquals(fixture.recordType, it.getDataType()); assertTrue(it.writeValues(listOf(fixture.records[2]))); assertEquals(listOf(fixture.records[2]), it.readValues())
                    }
                    requireNotNull(group.openGroup("nested")).use { nested ->
                        for ((name, type, input) in fixture.arrays) requireNotNull(nested.createArray(name, listOf(dimension), type)).use { array ->
                            assertEquals(type, array.getExtendedDataType()); assertTrue(array.writeValues(longArrayOf(0), longArrayOf(3), input))
                            assertEquals(type, array.getExtendedDataType()); assertEquals(input, values(array))
                            textAttribute(array, "kind", name)
                            requireNotNull(array.createAttribute("record", longArrayOf(), fixture.plainRecordType)).use { assertTrue(it.writeValues(listOf(fixture.plainRecord))) }
                        }
                        for (name in fixture.arrayNames) requireNotNull(nested.openArray(name)).use { array -> addJsonArrayMetadata(array, fixture) }
                    }
                }
            } }
        }
    }

    private suspend fun addJsonArrayMetadata(array: MDArray, fixture: JsonTreeFixture) {
        requireNotNull(array.createAttribute("json_meta", longArrayOf(3), fixture.type)).use {
            assertEquals(fixture.type, it.getDataType()); assertTrue(it.writeValues(fixture.values)); assertEquals(fixture.values, it.readValues())
        }
        requireNotNull(array.createAttribute("json_record", longArrayOf(), fixture.recordType)).use {
            assertEquals(fixture.recordType, it.getDataType()); assertTrue(it.writeValues(listOf(fixture.records[2]))); assertEquals(listOf(fixture.records[2]), it.readValues())
        }
    }

    private suspend fun assertJsonTree(result: Dataset) {
        val fixture = JsonTreeFixture()
        requireNotNull(result.getRootGroup()).use { root ->
            requireNotNull(root.getAttribute("json_config")).use { assertEquals(fixture.type, it.getDataType()); assertEquals(fixture.values, it.readValues()) }
            requireNotNull(root.openGroup("science")).use { group ->
                requireNotNull(group.getAttribute("json_record")).use { assertEquals(fixture.recordType, it.getDataType()); assertEquals(listOf(fixture.records[2]), it.readValues()) }
                requireNotNull(group.openGroup("nested")).use { nested ->
                    for ((name, type, expected) in fixture.arrays) requireNotNull(nested.openArray(name)).use { array ->
                        assertEquals(type, array.getExtendedDataType()); assertContentEquals(longArrayOf(3), shape(array)); assertEquals(expected, values(array))
                        requireNotNull(array.getAttribute("kind")).use { assertEquals(listOf(MDValue.Text(name)), it.readValues()) }
                        requireNotNull(array.getAttribute("record")).use { assertEquals(fixture.plainRecordType, it.getDataType()); assertEquals(listOf(fixture.plainRecord), it.readValues()) }
                    }
                    for (name in fixture.arrayNames) requireNotNull(nested.openArray(name)).use { array ->
                        requireNotNull(array.getAttribute("json_meta")).use { assertEquals(fixture.type, it.getDataType()); assertEquals(fixture.values, it.readValues()) }
                        requireNotNull(array.getAttribute("json_record")).use { assertEquals(fixture.recordType, it.getDataType()); assertEquals(listOf(fixture.records[2]), it.readValues()) }
                    }
                }
            }
        }
    }

    private suspend fun textAttribute(array: MDArray, name: String, value: String) {
        requireNotNull(array.createAttribute(name, longArrayOf(), MDDataType.Text())).use { assertTrue(it.writeValues(listOf(MDValue.Text(value)))) }
    }

    @Test fun arrayLocalDimensionsMaterializeWithPinnedCollisionAndReusePolicy() = runTest(timeout = 120.seconds) {
        val xml = """<VRTDataset><Group name="/"><Array name="a"><DataType>Int16</DataType><Dimension name="dim0" size="2"/><Dimension name="dim1" size="3"/><InlineValues>1 2 3 4 5 6</InlineValues></Array><Array name="b"><DataType>Int16</DataType><Dimension name="dim0" size="4"/><Dimension name="dim1" size="3"/><InlineValues>11 12 13 14 15 16 17 18 19 20 21 22</InlineValues></Array></Group></VRTDataset>"""
        val converted = fixture(xml) { source ->
            requireNotNull(source.getRootGroup()).use { group ->
                val registered = group.getDimensions()
                try { assertTrue(registered.isEmpty()) }
                finally { registered.forEach { it.close() } }
                for ((name, expectedShape) in listOf("a" to longArrayOf(2, 3), "b" to longArrayOf(4, 3))) {
                    requireNotNull(group.openArray(name)).use { array ->
                        assertContentEquals(expectedShape, shape(array))
                        assertEquals(listOf("dim0", "dim1"), array.getDimensions().map { dimension -> dimension.use { it.getName() } })
                    }
                }
            }
            source.multiDimTranslate()
        }
        // fixture has closed the source and removed its VRT workspace before readback.
        converted.use { result ->
            var retained: Dimension? = null
            try {
                requireNotNull(result.getRootGroup()).use { group ->
                    assertEquals(setOf("a", "b"), group.getArrayNames().toSet())
                    val materialized = group.getDimensions().associate { dimension -> dimension.use { it.getName() to it.getSize() } }
                    assertEquals(mapOf("dim0" to 2L, "dim1" to 3L, "b_dim0" to 4L), materialized)
                    for ((name, expectedShape, expectedValues) in listOf(
                        Triple("a", longArrayOf(2, 3), (1..6).map { MDValue.Numeric(it) }),
                        Triple("b", longArrayOf(4, 3), (11..22).map { MDValue.Numeric(it) }),
                    )) requireNotNull(group.openArray(name)).use { array ->
                        assertEquals(MDDataType.Numeric(GdalDataType.Int16), array.getExtendedDataType())
                        assertContentEquals(expectedShape, shape(array))
                        assertEquals(expectedValues, values(array))
                        assertEquals(if (name == "a") listOf("/dim0", "/dim1") else listOf("/b_dim0", "/dim1"),
                            array.getDimensions().map { dimension -> dimension.use { it.getFullName() } })
                    }
                    val dimensions = group.getDimensions()
                    try {
                        retained = dimensions.first()
                        assertTrue(requireNotNull(retained).getSize() > 0)
                    } finally { dimensions.filter { it !== retained }.forEach { it.close() } }
                }
                // The Dataset is still alive, but a group-owned dimension cannot outlive its Group.
                val dimension = requireNotNull(retained)
                assertFails { dimension.getSize() }
                dimension.close(); dimension.close()
                requireNotNull(result.getRootGroup()).use { assertEquals(setOf("a", "b"), it.getArrayNames().toSet()) }
            } finally { retained?.close() }
        }
    }

    @Test fun fileBackedTransposeThenViewProducesIndependentExactValues() = runTest(timeout = 120.seconds) {
        val result = fixture(cube()) { source -> source.multiDimTranslate("-array", "name=cube,dstname=selected,transpose=[2,0,1],view=[1:5:2,1,:]") }
        result.use { requireNotNull(it.getRootGroup()).use { group ->
            assertFalse("cube" in group.getArrayNames())
            requireNotNull(group.openArray("selected")).use { a ->
                assertContentEquals(longArrayOf(2, 4), shape(a))
                val expected = listOf(1, 3).flatMap { x -> (0 until 4).map { y -> MDValue.Numeric(100 + y * 10 + x + 0.25) } }
                assertEquals(expected, values(a))
            }
            requireNotNull(group.getAttribute("Conventions")).use { assertEquals(listOf(MDValue.Text("CF-1.8")), it.readValues()) }
        } }
    }

    @Test fun coordinateSubsetAndOddScaleAxisUsePinnedGdalIndexPolicy() = runTest(timeout = 120.seconds) {
        fixture(cube()) { source ->
        source.multiDimTranslate("-array", "cube", "-subset", "time(10,20)", "-scaleaxes", "x(2)").use { result ->
            requireNotNull(result.getRootGroup()).use { group -> requireNotNull(group.openArray("cube")).use { a ->
                // Pinned GDAL divides 5 by 2 with integer truncation: indices 0,2; index4 is omitted.
                assertContentEquals(longArrayOf(2, 4, 2), shape(a))
                val expected = (0..1).flatMap { t -> (0 until 4).flatMap { y -> listOf(0, 2).map { x -> MDValue.Numeric(t * 100 + y * 10 + x + 0.25) } } }
                assertEquals(expected, values(a))
                val dimensions = a.getDimensions()
                try {
                    requireNotNull(dimensions[0].getIndexingVariable()).use { assertEquals(listOf(MDValue.Numeric(10), MDValue.Numeric(20)), values(it)) }
                    requireNotNull(dimensions[2].getIndexingVariable()).use { assertEquals(listOf(MDValue.Numeric(0), MDValue.Numeric(2)), values(it)) }
                } finally { dimensions.forEach { it.close() } }
            } }
        }
        }
        val dates = """<VRTDataset><Group name="/"><Dimension name="date" size="3" indexingVariable="date"/><Array name="date"><DataType>String</DataType><DimensionRef ref="/date"/><InlineValuesWithValueElement><Value>2024-01-01</Value><Value>2024-01-02</Value><Value>2024-01-03</Value></InlineValuesWithValueElement></Array><Array name="values"><DataType>Int16</DataType><DimensionRef ref="/date"/><InlineValues>9 13 17</InlineValues></Array></Group></VRTDataset>"""
        fixture(dates) { source ->
            source.multiDimTranslate("-array", "values", "-subset", "date(\"2024-01-02\",\"2024-01-03\")").use { result ->
                requireNotNull(result.getRootGroup()).use { root -> requireNotNull(root.openArray("values")).use { array ->
                    assertContentEquals(longArrayOf(2), shape(array)); assertEquals(listOf(MDValue.Numeric(13), MDValue.Numeric(17)), values(array))
                    array.getDimensions().single().use { dimension -> requireNotNull(dimension.getIndexingVariable()).use {
                        assertEquals(listOf(MDValue.Text("2024-01-02"), MDValue.Text("2024-01-03")), values(it))
                    } }
                } }
            }
        }
    }

    @Test fun selectedGroupsFollowSingleRootFlatteningAndRepeatedDestinationNames() = runTest(timeout = 120.seconds) { fixture(cube()) { source ->
        source.multiDimTranslate("-group", "name=/science,dstname=selected").use { result -> requireNotNull(result.getRootGroup()).use { root ->
            // Pinned GDAL lifts a single group's contents to root, ignoring its dstname.
            // Choosing a subgroup intentionally excludes attributes on the unselected source root.
            assertEquals(emptyList(), root.getGroupNames()); assertNull(root.getAttribute("Conventions"))
            requireNotNull(root.getAttribute("label")).use { assertEquals(listOf(MDValue.Text("observations")), it.readValues()) }
            requireNotNull(root.openArray("small")).use { assertEquals(listOf(MDValue.Numeric(7), MDValue.Numeric(11)), values(it)) }
        } }
        source.multiDimTranslate("-group", "name=/science,dstname=selected", "-group", "name=/science,dstname=second").use { result -> requireNotNull(result.getRootGroup()).use { root ->
            assertEquals(setOf("selected", "second"), root.getGroupNames().toSet()); assertNull(root.getAttribute("Conventions"))
            for (name in listOf("selected", "second")) requireNotNull(root.openGroup(name)).use { group ->
                requireNotNull(group.getAttribute("label")).use { assertEquals(listOf(MDValue.Text("observations")), it.readValues()) }
                requireNotNull(group.openArray("small")).use { assertEquals(listOf(MDValue.Numeric(7), MDValue.Numeric(11)), values(it)) }
            }
        } }
    } }

    @Test fun gtiffFilesAndDirectOutputsHaveExactPixelsAndPreserveExistingOutput() = runTest(timeout = 120.seconds) { fixture(plain()) { source ->
        val files = source.multiDimTranslateFiles("-array", "values", name = "plane.tif")
        assertTrue(files.files.any { it.name == "plane.tif" })
        r().openDataset(files).use { assertRaster(it) }
        val classic = r().openDataset(files)
        val independent = classic.use { raster ->
            assertRaster(raster)
            raster.multiDimTranslate("-array", "band=1")
        }
        // Closing the classic input also removes its temporary file workspace. The MEM
        // conversion must own its values rather than retaining a lazy reference to that file.
        assertFails { classic.getRasterCount() }
        independent.use { result -> requireNotNull(result.getRootGroup()).use { group ->
            requireNotNull(group.openArray("Band1")).use { array ->
                assertEquals(GdalDataType.Float64, array.getDataType())
                assertContentEquals(longArrayOf(4, 5), shape(array))
                assertEquals((0 until 20).map { MDValue.Numeric(it + 0.25) }, values(array))
            }
        } }
        val path = datasetOutputPath("multidimensional-direct.tif")
        requireNotNull(r().getDriver("GTiff")).use { driver -> try {
            source.multiDimTranslateTo(path, "-array", "values")
            r().openDataset(path).use { assertRaster(it) }
            assertFails { source.multiDimTranslateTo(path, "-array", "values") }
            r().openDataset(path).use { assertRaster(it) }
        } finally { driver.delete(path) } }
        requireNotNull(source.getRootGroup()).use { group -> requireNotNull(group.openArray("values")).use { assertContentEquals(longArrayOf(4, 5), shape(it)) } }
    } }
    private suspend fun assertRaster(dataset: Dataset) {
        assertEquals(5, dataset.getRasterXSize()); assertEquals(4, dataset.getRasterYSize()); assertEquals(1, dataset.getRasterCount())
        val output = DoubleArray(20); assertEquals(0, dataset.readRaster(output))
        assertContentEquals(DoubleArray(20) { it + 0.25 }, output)
    }

    @Test fun unsupportedLossOptionsAndAnonymousSelectorsFailWithoutChangingSource() = runTest(timeout = 120.seconds) {
        fixture(cube()) { source ->
        val invalid = listOf(listOf("-of", "GTiff"), listOf("-update"), listOf("-array"), listOf("-array", "missing"),
            listOf("-array", "name=cube,transpose=[0,0,1]"), listOf("-array", "name=cube,ot=Float32"),
            listOf("-array", "name=cube,resample=true"), listOf("-array", "name=cube,view=[1,:,:]", "-subset", "x(1,2)"),
            listOf("-scaleaxes", "x(0)"), listOf("-subset", "time(bad)"), listOf("-group", "/missing"), listOf("unexpected-output.tif"))
        for (args in invalid) assertFails { source.multiDimTranslate(arguments = args) }
        assertFails { source.multiDimTranslateFiles("-array", "cube") } // 3D and extra metadata cannot silently become GTiff.
        source.multiDimTranslate().use { memory ->
            assertFails { memory.multiDimTranslate("-array", "cube") }
            assertFails { memory.multiDimTranslate("-group", "/science") }
        }
        for (format in listOf("MEM", "VRT", "missing")) assertFails { source.multiDimTranslateFiles(driver = format) }
        assertFails { source.multiDimTranslateFiles(name = "../bad.tif") }; assertFails { source.multiDimTranslateTo("") }
        requireNotNull(source.getRootGroup()).use { group -> requireNotNull(group.openArray("cube")).use { assertEquals(60, values(it).size) } }
        }
        val wide = """<VRTDataset><Group name="/"><Dimension name="n" size="3" indexingVariable="n"/><Array name="n"><DataType>Int64</DataType><DimensionRef ref="/n"/><InlineValues>9007199254740992 9007199254740993 9007199254740994</InlineValues></Array><Array name="values"><DataType>Int16</DataType><DimensionRef ref="/n"/><InlineValues>3 5 7</InlineValues></Array></Group></VRTDataset>"""
        fixture(wide) { source ->
            assertFails { source.multiDimTranslate("-array", "values", "-subset", "n(9007199254740993)") }
            source.multiDimTranslate("-array", "name=values,view=[1:2]").use { result -> requireNotNull(result.getRootGroup()).use { root ->
                requireNotNull(root.openArray("values")).use { a ->
                    assertEquals(listOf(MDValue.Numeric(5)), values(a))
                    a.getDimensions().single().use { dim -> requireNotNull(dim.getIndexingVariable()).use { axis ->
                        assertEquals(listOf(MDValue.Numeric("9007199254740993")), values(axis))
                    } }
                }
            } }
        }
    }

    @Test fun cancellationCallbackFailureAndPartialOutputOwnershipAllowReuse() = runTest(timeout = 120.seconds) { fixture(plain()) { source ->
        assertFailsWith<CancellationException> { withGdalProgress({ false }) { source.multiDimTranslate() } }
        val failure = IllegalStateException("multidimensional callback")
        val actual = assertFailsWith<IllegalStateException> { withGdalProgress({ throw failure }) { source.multiDimTranslateFiles("-array", "values") } }
        assertTrue(actual === failure || actual.cause === failure)
        val path = datasetOutputPath("multidimensional-cancelled.tif")
        requireNotNull(r().getDriver("GTiff")).use { driver -> try {
            assertFailsWith<CancellationException> { withGdalProgress({ false }) { source.multiDimTranslateTo(path, "-array", "values") } }
            // Cancellation may occur before creation or after partial output. Caller owns cleanup.
        } finally { driver.delete(path) } }
        source.multiDimTranslate().use { copy -> requireNotNull(copy.getRootGroup()).use { group -> requireNotNull(group.openArray("values")).use { assertEquals(20, values(it).size) } } }
        r().openDataset(source.multiDimTranslateFiles("-array", "values")).use { assertRaster(it) }
    } }
}

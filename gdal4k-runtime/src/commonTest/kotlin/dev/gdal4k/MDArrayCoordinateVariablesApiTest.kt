package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class MDArrayCoordinateVariablesApiTest {
    private suspend fun memory(block: suspend (Dataset, Group) -> Unit) {
        requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
            requireNotNull(driver.createMultidimensional("")).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { block(dataset, it) }
            }
        }
    }

    private suspend fun put(group: Group, name: String, dimensions: List<Dimension>, type: MDDataType,
                            shape: LongArray, values: List<MDValue>) {
        requireNotNull(group.createArray(name, dimensions, type)).use {
            assertTrue(it.writeValues(LongArray(shape.size), shape, values))
        }
    }

    private suspend fun association(array: MDArray, names: String) {
        val attribute = array.getAttribute("coordinates")
            ?: requireNotNull(array.createAttribute("coordinates", longArrayOf(), MDDataType.Text()))
        attribute.use { assertTrue(it.writeValues(listOf(MDValue.Text(names)))) }
    }

    private suspend fun coordinates(array: MDArray, block: suspend (List<MDArray>) -> Unit) {
        val result = array.getCoordinateVariables()
        try { block(result) }
        finally { withContext(NonCancellable) { for (coordinate in result.asReversed()) coordinate.close() } }
    }

    private suspend fun shape(array: MDArray): LongArray {
        val sizes = mutableListOf<Long>()
        for (dimension in array.getDimensions()) dimension.use { sizes.add(it.getSize()) }
        return sizes.toLongArray()
    }

    private fun numeric(vararg values: String): List<MDValue> = values.map { MDValue.Numeric(it) }

    @Test fun emptyListsAreDistinctFromIndexingVariablesAndMalformedAssociationMetadata() = runTest(timeout = 120.seconds) {
        memory { _, group ->
            requireNotNull(group.createArray("scalar", emptyList(), GdalDataType.Int16)).use {
                assertEquals(emptyList(), it.getCoordinateVariables())
            }
            requireNotNull(group.createDimension("n", 2)).use { dimension ->
                requireNotNull(group.createArray("axis", listOf(dimension), GdalDataType.Int64)).use { axis ->
                    val values = numeric("9007199254740993", "9007199254740995")
                    assertTrue(axis.writeValues(longArrayOf(0), longArrayOf(2), values))
                    assertTrue(dimension.setIndexingVariable(axis))
                    requireNotNull(group.createArray("values", listOf(dimension), GdalDataType.Int16)).use { source ->
                        assertEquals(emptyList(), source.getCoordinateVariables())
                        for (actual in source.getDimensions()) actual.use { found ->
                            requireNotNull(found.getIndexingVariable()).use {
                                assertEquals(values, it.readValues(longArrayOf(0), longArrayOf(2)))
                            }
                        }
                        requireNotNull(source.createAttribute("coordinates", longArrayOf(), MDDataType.Numeric(GdalDataType.Int16))).use {
                            assertTrue(it.writeValues(listOf(MDValue.Numeric(7))))
                        }
                        assertEquals(emptyList(), source.getCoordinateVariables())
                        assertTrue(source.deleteAttribute("coordinates"))
                        requireNotNull(source.createAttribute("coordinates", longArrayOf(1), MDDataType.Text())).use {
                            assertTrue(it.writeValues(listOf(MDValue.Text("axis"))))
                        }
                        assertEquals(emptyList(), source.getCoordinateVariables())
                    }
                }
            }
        }
    }

    @Test fun heterogeneousCoordinatesRetainDriverOrderShapeTypesAndExactValues() = runTest(timeout = 120.seconds) {
        memory { _, group ->
            requireNotNull(group.createDimension("y", 2)).use { y ->
                requireNotNull(group.createDimension("x", 3)).use { x ->
                    requireNotNull(group.createDimension("n", 2)).use { n ->
                        val signed = numeric("-9007199254740993", "9007199254740993")
                        val unsigned = numeric("9007199254740993", "18446744073709551615")
                        val longitude = numeric("0.25", "0.5", "0.75", "1.25", "1.5", "1.75")
                        val latitude = numeric("-0.25", "-0.5", "-0.75", "-1.25", "-1.5", "-1.75")
                        val labels = listOf(MDValue.Text(null), MDValue.Text("한글 😀"))
                        put(group, "signed", listOf(n), MDDataType.Numeric(GdalDataType.Int64), longArrayOf(2), signed)
                        put(group, "unsigned", listOf(n), MDDataType.Numeric(GdalDataType.UInt64), longArrayOf(2), unsigned)
                        put(group, "longitude", listOf(y, x), MDDataType.Numeric(GdalDataType.Float64), longArrayOf(2, 3), longitude)
                        put(group, "latitude", listOf(y, x), MDDataType.Numeric(GdalDataType.Float64), longArrayOf(2, 3), latitude)
                        put(group, "표지", listOf(n), MDDataType.Text(), longArrayOf(2), labels)
                        requireNotNull(group.createArray("values", listOf(y, x), GdalDataType.Int16)).use { source ->
                            association(source, "signed longitude 표지 latitude unsigned")
                            coordinates(source) { result ->
                                assertEquals(listOf("signed", "longitude", "표지", "latitude", "unsigned"), result.map { it.getName() })
                                val expected = listOf(
                                    Triple(MDDataType.Numeric(GdalDataType.Int64), longArrayOf(2), signed),
                                    Triple(MDDataType.Numeric(GdalDataType.Float64), longArrayOf(2, 3), longitude),
                                    Triple(MDDataType.Text(), longArrayOf(2), labels),
                                    Triple(MDDataType.Numeric(GdalDataType.Float64), longArrayOf(2, 3), latitude),
                                    Triple(MDDataType.Numeric(GdalDataType.UInt64), longArrayOf(2), unsigned))
                                for ((array, wanted) in result.zip(expected)) {
                                    assertEquals(wanted.first, array.getExtendedDataType())
                                    assertContentEquals(wanted.second, shape(array))
                                    assertEquals(wanted.third, array.readValues(LongArray(wanted.second.size), wanted.second))
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    @Test fun duplicatesAndSelfReferencesHaveIndependentWrappersAndLiveWritableAliases() = runTest(timeout = 120.seconds) {
        memory { _, group ->
            requireNotNull(group.createDimension("n", 2)).use { dimension ->
                requireNotNull(group.createArray("axis", listOf(dimension), GdalDataType.Int64)).use { axis ->
                    assertTrue(axis.writeValues(longArrayOf(0), longArrayOf(2), numeric("1", "2")))
                    requireNotNull(group.createArray("values", listOf(dimension), GdalDataType.Int16)).use { source ->
                        assertTrue(source.writeValues(longArrayOf(0), longArrayOf(2), numeric("7", "8")))
                        association(source, "axis values axis")
                        coordinates(source) { result ->
                            assertEquals(listOf("axis", "values", "axis"), result.map { it.getName() })
                            assertNotSame(result[0], result[2])
                            result[0].close(); result[0].close()
                            val changed = numeric("9007199254740993", "9007199254740995")
                            assertTrue(result[2].writeValues(longArrayOf(0), longArrayOf(2), changed))
                            assertEquals(changed, axis.readValues(longArrayOf(0), longArrayOf(2)))
                            requireNotNull(group.openArray("axis")).use {
                                assertEquals(changed, it.readValues(longArrayOf(0), longArrayOf(2)))
                            }
                            assertTrue(result[1].writeValues(longArrayOf(0), longArrayOf(2), numeric("9", "10")))
                            result[1].close()
                            assertEquals(numeric("9", "10"), source.readValues(longArrayOf(0), longArrayOf(2)))
                            assertEquals(changed, result[2].readValues(longArrayOf(0), longArrayOf(2)))
                            assertEquals("values", source.getName())
                        }
                    }
                }
            }
        }
    }

    @Test fun missingNamesWarnAndSkipWithoutDiscardingOtherCoordinates() = runTest(timeout = 120.seconds) {
        memory { _, group ->
            requireNotNull(group.createDimension("n", 1)).use { dimension ->
                put(group, "north", listOf(dimension), MDDataType.Numeric(GdalDataType.Int16), longArrayOf(1), numeric("11"))
                put(group, "south", listOf(dimension), MDDataType.Numeric(GdalDataType.Int16), longArrayOf(1), numeric("22"))
                requireNotNull(group.createArray("values", listOf(dimension), GdalDataType.Int16)).use { source ->
                    association(source, "missing north other_missing south north")
                    coordinates(source) { result ->
                        assertEquals(listOf("north", "south", "north"), result.map { it.getName() })
                        assertEquals(listOf(numeric("11"), numeric("22"), numeric("11")),
                            result.map { it.readValues(longArrayOf(0), longArrayOf(1)) })
                    }
                    association(source, "missing other_missing")
                    assertEquals(emptyList(), source.getCoordinateVariables())
                    association(source, "south")
                    coordinates(source) { result ->
                        assertEquals(listOf("south"), result.map { it.getName() })
                        assertEquals(numeric("22"), result.single().readValues(longArrayOf(0), longArrayOf(1)))
                    }
                    assertEquals("values", source.getName())
                }
            }
        }
    }

    @Test fun associationChangesAffectLaterQueriesWithoutRetargetingEarlierHandles() = runTest(timeout = 120.seconds) {
        memory { _, group ->
            requireNotNull(group.createDimension("n", 2)).use { dimension ->
                put(group, "first", listOf(dimension), MDDataType.Numeric(GdalDataType.Int32), longArrayOf(2), numeric("1", "2"))
                put(group, "second", listOf(dimension), MDDataType.Numeric(GdalDataType.Int32), longArrayOf(2), numeric("3", "4"))
                requireNotNull(group.createArray("values", listOf(dimension), GdalDataType.Int16)).use { source ->
                    association(source, "first second")
                    coordinates(source) { original ->
                        association(source, "second first second")
                        coordinates(source) { current ->
                            assertEquals(listOf("first", "second"), original.map { it.getName() })
                            assertEquals(listOf("second", "first", "second"), current.map { it.getName() })
                            assertTrue(original[0].writeValues(longArrayOf(0), longArrayOf(2), numeric("9", "10")))
                            assertEquals(numeric("9", "10"), current[1].readValues(longArrayOf(0), longArrayOf(2)))
                            current[0].close()
                            assertEquals(numeric("3", "4"), original[1].readValues(longArrayOf(0), longArrayOf(2)))
                            assertEquals(numeric("3", "4"), current[2].readValues(longArrayOf(0), longArrayOf(2)))
                        }
                    }
                }
            }
        }
    }

    @Test fun sourceAndAncestorClosureInvalidateCoordinateHandlesAndDescendants() = runTest(timeout = 120.seconds) {
        for (owner in listOf("source", "group", "dataset")) memory { dataset, group ->
            requireNotNull(group.createDimension("n", 2)).use { dimension ->
                put(group, "axis", listOf(dimension), MDDataType.Numeric(GdalDataType.Int16), longArrayOf(2), numeric("1", "2"))
                requireNotNull(group.createArray("values", listOf(dimension), GdalDataType.Int16)).use { source ->
                    association(source, "axis axis")
                    coordinates(source) { result ->
                        result.first().getDimensions().single().use { coordinateDimension ->
                            result.first().getView("[:]").use { descendant ->
                                when (owner) {
                                    "source" -> source.close()
                                    "group" -> group.close()
                                    "dataset" -> dataset.close()
                                }
                                for (coordinate in result) assertFailsWith<IllegalStateException> { coordinate.getName() }
                                assertFailsWith<IllegalStateException> { coordinateDimension.getSize() }
                                assertFailsWith<IllegalStateException> { descendant.readValues(longArrayOf(0), longArrayOf(2)) }
                                if (owner == "source") requireNotNull(group.openArray("axis")).use {
                                    assertEquals(numeric("1", "2"), it.readValues(longArrayOf(0), longArrayOf(2)))
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

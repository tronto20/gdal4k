package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.math.*
import kotlin.time.Duration.Companion.seconds

class CoordinateArraysApiTest {
    private suspend fun <T> transform(block:suspend (CoordinateTransformation)->T):T {
        val runtime=Gdal4k.runtime()
        try {return runtime.createSpatialReference("EPSG:4326").use { source ->
            runtime.createSpatialReference("EPSG:3857").use { target ->
                source.setAxisMappingStrategy(0);target.setAxisMappingStrategy(0)
                requireNotNull(runtime.createCoordinateTransformation(source,target)).use { block(it) }
            }
        }} finally {Gdal4k.close()}
    }
    @Test fun mixedDimensionsAndChunkBoundaries()=runTest(timeout=120.seconds) { transform { ct ->
        val points=Array(3075) { i -> when {
            i<1025 -> doubleArrayOf(10.0+i*0.001,45.0)
            i<2050 -> doubleArrayOf(10.0,45.0,i*0.25)
            else -> doubleArrayOf(10.0,45.0,i*0.25,2020.0)
        } };val before=points.map {it.copyOf()};val rows=points.copyOf();ct.transformPoints(points)
        for(i in points.indices) {
            assertSame(rows[i],points[i]);assertEquals(before[i].size,points[i].size)
            assertEquals(before[i][0]*20037508.342789244/180.0,points[i][0],1e-6)
            assertEquals(ln(tan((90.0+before[i][1])*PI/360.0))*6378137.0,points[i][1],1e-6)
            if(points[i].size>2)assertEquals(before[i][2],points[i][2])
            if(points[i].size==4)assertEquals(before[i][3],points[i][3])
        }
    } }
    @Test fun invalidShapePreservesEveryPoint()=runTest { transform { ct ->
        for(invalid in listOf(doubleArrayOf(1.0),DoubleArray(5))) {
            val points=arrayOf(doubleArrayOf(10.0,45.0),invalid);val before=points.map {it.copyOf()}
            assertFails {ct.transformPoints(points)};points.forEachIndexed {i,p ->assertContentEquals(before[i],p)}
        }
        ct.transformPoints(arrayOf(doubleArrayOf(10.0,45.0)))
    } }
    @Test fun failureAfterEarlierChunkDoesNotCommit()=runTest(timeout=120.seconds) { transform { ct ->
        val points=Array(2050) { doubleArrayOf(10.0,if(it==2049)1000.0 else 45.0) };val before=points.map {it.copyOf()}
        assertFails {ct.transformPoints(points)};points.forEachIndexed {i,p ->assertContentEquals(before[i],p)}
        val valid=arrayOf(doubleArrayOf(10.0,45.0));ct.transformPoints(valid);assertTrue(valid[0][0]>1_000_000)
    } }
    @Test fun emptyAndRepeatedRowReferences()=runTest { transform { ct ->
        ct.transformPoints(emptyArray());val point=doubleArrayOf(10.0,45.0);val points=arrayOf(point,point)
        ct.transformPoints(points);assertSame(point,points[0]);assertSame(point,points[1]);assertEquals(1113194.9079327357,point[0],1e-6)
    } }
    @Test fun cancelledCallerLeavesInputAndHandleUsable()=runTest { transform { ct ->
        val points=arrayOf(doubleArrayOf(10.0,45.0));val before=points[0].copyOf()
        val job=launch(start=CoroutineStart.LAZY) {ct.transformPoints(points)};job.cancel();job.join();assertContentEquals(before,points[0])
        ct.transformPoints(points);assertTrue(points[0][0]>1_000_000)
    } }
    @Test fun progressCancellationDoesNotCommit()=runTest(timeout=120.seconds) { transform { ct ->
        val points=Array(131072) {doubleArrayOf(10.0,45.0)};var called=false
        assertFailsWith<CancellationException> {withGdalProgress({called=true;false}) {ct.transformPoints(points)}}
        assertTrue(called);assertTrue(points.all {it[0]==10.0 && it[1]==45.0})
        ct.transformPoints(arrayOf(doubleArrayOf(10.0,45.0)))
    } }
    @Test fun closedTransformationRejectsEmptyAndNonempty()=runTest { transform { ct ->
        ct.close();assertFails {ct.transformPoints(emptyArray())};assertFails {ct.transformPoints(arrayOf(doubleArrayOf(1.0,2.0)))}
    } }
}

package dev.gdal4k.internal

import dev.gdal4k.GdalBinaryCall
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.yield
import kotlinx.serialization.json.*

/** Preserves the public nested-array API and uses private input/output storage until final commit. */
internal suspend fun RuntimeHandle.coordinateCall(points: Array<DoubleArray>) {
    checkOpen();currentCoroutineContext().ensureActive()
    require(points.all { it.size in 2..4 }) { "A point must have two, three or four coordinates" }
    if(!engine.supportsBinaryCoordinates || points.size>Int.MAX_VALUE/40) {
        val result=invoke("CoordinateTransformation","transformPoints_Array_DoubleArray",mapOf("points" to encode(points))).jsonArray
        require(result.size==points.size) { "Invalid coordinate response count" }
        val decoded=Array(result.size) { i -> result[i].jsonArray.let { row ->
            require(row.size==points[i].size) { "Invalid coordinate response dimensions" }
            DoubleArray(row.size) { j -> row[j].jsonPrimitive.content.toDouble() }
        } }
        currentCoroutineContext().ensureActive()
        for(i in points.indices)decoded[i].copyInto(points[i])
        return
    }
    val call=GdalBinaryCall(prepare={
        val flat=DoubleArray(points.size*5)
        for(i in points.indices) {
            if(i and 4095==0) {currentCoroutineContext().ensureActive();yield()}
            flat[i*5]=points[i].size.toDouble();points[i].copyInto(flat,i*5+1)
        }
        packRaster(flat)
    },complete={ value,bytes ->
        require(value.jsonPrimitive.int==points.size && bytes.size==points.size*40) { "Invalid binary coordinate response length" }
        val flat=DoubleArray(points.size*5);unpackRaster(bytes,flat)
        for(i in points.indices) {
            if(i and 4095==0) {currentCoroutineContext().ensureActive();yield()}
            require(flat[i*5]==points[i].size.toDouble()) { "Invalid binary coordinate response dimensions" }
        }
        // No suspension during final copy: cancellation racing this commit can observe completed output.
        currentCoroutineContext().ensureActive()
        for(i in points.indices)flat.copyInto(points[i],0,i*5+1,i*5+1+points[i].size)
    })
    engine.invoke("CoordinateTransformation",runtimeId,"transformPoints_Array_DoubleArray",mapOf("pointCount" to JsonPrimitive(points.size)),binary=call)
}

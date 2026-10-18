package dev.gdal4k.runtime

/**
 * Shared coordinate transformation handle.
 */
interface CoordinateTransformation {
    suspend fun close()

    suspend fun transformPoint(x: Double, y: Double): DoubleArray

    suspend fun transformPoint(x: Double, y: Double, z: Double): DoubleArray

    /** In-place 2D/3D/4D transform. Each row retains its dimensions and identity.
     * Bundled engines batch GDAL calls and negotiate binary transfer; older engines use JSON.
     * Failure/cancellation before final commit leaves caller coordinates unchanged. Cancellation
     * racing the final copy may observe completed output. Do not concurrently mutate the arrays.
     * GDAL controls axis order, operation selection and 4D time semantics; coordinates are not repaired.
     */
    suspend fun transformPoints(points: Array<DoubleArray>)

    suspend fun transformBounds(
        bounds: DoubleArray,
        xMin: Double,
        yMin: Double,
        xMax: Double,
        yMax: Double,
        densifyPoints: Int = 0,
    )

    suspend fun getInverse(): CoordinateTransformation?
}

suspend inline fun <T : CoordinateTransformation, R> T.use(block: (T) -> R): R {
    try {
        return block(this)
    } finally {
        kotlinx.coroutines.withContext(kotlinx.coroutines.NonCancellable) { close() }
    }
}

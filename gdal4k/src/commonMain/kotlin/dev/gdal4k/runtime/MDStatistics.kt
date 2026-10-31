package dev.gdal4k.runtime

/**
 * Detached statistics of the queried multidimensional array's valid samples.
 *
 * Moments use native Double arithmetic, including rounding for large integer values and
 * possible nonfinite results. The unsigned sample count remains exact. With no valid samples,
 * GDAL's generic implementation returns count 0, minimum Double.MAX_VALUE, maximum
 * -Double.MAX_VALUE, mean 0 and standard deviation 0. This is a successful result.
 * The snapshot remains usable after its source closes and owns no native resource.
 */
data class MDStatistics(
    val minimum: Double,
    val maximum: Double,
    val mean: Double,
    val standardDeviation: Double,
    val validCount: ULong,
)

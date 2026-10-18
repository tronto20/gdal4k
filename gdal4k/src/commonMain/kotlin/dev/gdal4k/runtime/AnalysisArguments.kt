package dev.gdal4k.runtime

import dev.gdal4k.GdalFiles

suspend fun Band.proximityTo(target: Band, vararg arguments: String): Int =
    proximityTo(target, arguments.asList())

suspend fun Band.fillNoData(
    vararg arguments: String, maxSearchDistance: Double = 100.0,
    smoothingIterations: Int = 0, mask: Band? = null,
): Int = fillNoData(maxSearchDistance, smoothingIterations, mask, arguments.asList())

suspend fun Dataset.contour(
    interval: Double, vararg arguments: String, base: Double = 0.0, band: Int = 1,
    driver: String = "GPKG", name: String = "contours.gpkg", layerName: String = "contours",
): GdalFiles = contour(interval, base, band, driver, name, layerName, arguments.asList())

suspend fun Dataset.contourTo(
    path: String, interval: Double, vararg arguments: String, base: Double = 0.0,
    band: Int = 1, driver: String = "GPKG", layerName: String = "contours",
) = contourTo(path, interval, base, band, driver, layerName, arguments.asList())

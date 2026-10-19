package dev.gdal4k.runtime

import dev.gdal4k.GdalFiles

/** GDAL footprint options; driver, output and layer name are named settings. */
suspend fun Dataset.footprint(
    vararg arguments: String, driver: String = "GPKG", name: String = "footprint.gpkg",
    layerName: String = "footprint",
): GdalFiles = footprint(driver, name, layerName, arguments.asList())

suspend fun Dataset.footprintTo(
    path: String, vararg arguments: String, driver: String = "GPKG", layerName: String = "footprint",
) = footprintTo(path, driver, layerName, arguments.asList())

package dev.gdal4k.runtime

import dev.gdal4k.GdalFiles

suspend fun GdalRuntime.tileIndex(
    sources: List<String>, vararg arguments: String, driver: String = "GPKG", name: String = "tiles.gpkg",
    layerName: String = "tiles", locationField: String = "location", absolutePaths: Boolean = false,
): GdalFiles = tileIndex(sources, driver, name, layerName, locationField, absolutePaths, arguments.asList())

suspend fun GdalRuntime.tileIndexTo(
    sources: List<String>, path: String, vararg arguments: String, driver: String = "GPKG",
    layerName: String = "tiles", locationField: String = "location", absolutePaths: Boolean = false,
) = tileIndexTo(sources, path, driver, layerName, locationField, absolutePaths, arguments.asList())

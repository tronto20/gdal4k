package dev.gdal4k.runtime

import dev.gdal4k.GdalFiles

/**
 * GDAL utility options as positional strings. Parameters after [arguments], such as
 * driver and output name, must be named. The List-based member overloads remain
 * available for dynamically assembled options.
 *
 * asList() views the vararg array without making another element copy. The bundled
 * runtime encodes options before its first suspension; these helpers retain no view.
 */
suspend fun GdalRuntime.buildVRT(sources: List<Dataset>, vararg arguments: String): Dataset =
    buildVRT(sources, arguments.asList())

suspend fun GdalRuntime.warp(
    sources: List<Dataset>, target: String, vararg arguments: String,
    driver: String = "GTiff", name: String = "warped.tif",
): GdalFiles = warp(sources, target, driver, name, arguments.asList())

suspend fun GdalRuntime.warpTo(
    sources: List<Dataset>, path: String, target: String, vararg arguments: String,
    driver: String = "GTiff",
) = warpTo(sources, path, target, driver, arguments.asList())

suspend fun Dataset.info(vararg arguments: String): String = info(arguments.asList())

suspend fun Dataset.multiDimTranslate(vararg arguments: String): Dataset =
    multiDimTranslate(arguments.asList())

suspend fun Dataset.multiDimTranslateFiles(
    vararg arguments: String, driver: String = "GTiff", name: String = "converted.tif",
): GdalFiles = multiDimTranslateFiles(driver, name, arguments.asList())

suspend fun Dataset.multiDimTranslateTo(
    path: String, vararg arguments: String, driver: String = "GTiff",
) = multiDimTranslateTo(path, driver, arguments.asList())

suspend fun Dataset.translate(
    vararg arguments: String, driver: String = "GTiff", name: String = "output.tif",
): GdalFiles = translate(driver, name, arguments.asList())

suspend fun Dataset.translateTo(
    path: String, vararg arguments: String, driver: String = "GTiff",
) = translateTo(path, driver, arguments.asList())

suspend fun Dataset.warp(
    target: String, vararg arguments: String, driver: String = "GTiff", name: String = "warped.tif",
): GdalFiles = warp(target, driver, name, arguments.asList())

suspend fun Dataset.warpTo(
    path: String, target: String, vararg arguments: String, driver: String = "GTiff",
) = warpTo(path, target, driver, arguments.asList())

suspend fun Dataset.translateVector(
    vararg arguments: String, driver: String = "GeoJSON", name: String = "output.geojson",
): GdalFiles = translateVector(driver, name, arguments.asList())

suspend fun Dataset.translateVectorTo(
    path: String, vararg arguments: String, driver: String = "GeoJSON",
) = translateVectorTo(path, driver, arguments.asList())

suspend fun Dataset.rasterize(
    vararg arguments: String, driver: String = "GTiff", name: String = "rasterized.tif",
): GdalFiles = rasterize(driver, name, arguments.asList())

suspend fun Dataset.rasterizeTo(
    path: String, vararg arguments: String, driver: String = "GTiff",
) = rasterizeTo(path, driver, arguments.asList())

suspend fun Dataset.demProcessing(
    processing: String, vararg arguments: String, driver: String = "GTiff", name: String = "terrain.tif",
): GdalFiles = demProcessing(processing, driver, name, arguments.asList())

suspend fun Dataset.demProcessingTo(
    path: String, processing: String, vararg arguments: String, driver: String = "GTiff",
) = demProcessingTo(path, processing, driver, arguments.asList())

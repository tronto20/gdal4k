package dev.gdal4k.runtime

import dev.gdal4k.GdalFiles

enum class ViewshedMode(val code: Int) { Diagonal(1), Edge(2), Max(3), Min(4) }
enum class ViewshedOutput(val code: Int) { Visibility(1), MinimumHeightFromDem(2), MinimumHeightFromGround(3) }

/** Observer and target heights are relative to the DEM. A zero maxDistance uses the entire
 * raster. curvatureCoefficient=0 disables curvature/refraction. Negative noDataValue omits
 * output NoData metadata. Visible/invisible values do not affect height outputs, but pinned
 * GDAL still requires them to be in the Byte range.
 */
data class ViewshedOptions(
    val observerX: Double,
    val observerY: Double,
    val observerHeight: Double = 2.0,
    val targetHeight: Double = 0.0,
    val maxDistance: Double = 0.0,
    val curvatureCoefficient: Double = 0.85714,
    val mode: ViewshedMode = ViewshedMode.Edge,
    val output: ViewshedOutput = ViewshedOutput.Visibility,
    val visibleValue: Double = 255.0,
    val invisibleValue: Double = 0.0,
    val outOfRangeValue: Double = 0.0,
    val noDataValue: Double = -1.0,
)

suspend fun Dataset.grid(vararg arguments: String, driver: String = "GTiff", name: String = "grid.tif"): GdalFiles =
    grid(driver, name, arguments.asList())
suspend fun Dataset.gridTo(path: String, vararg arguments: String, driver: String = "GTiff") =
    gridTo(path, driver, arguments.asList())
suspend fun Dataset.viewshed(options: ViewshedOptions, vararg creationOptions: String, band: Int = 1, driver: String = "GTiff", name: String = "viewshed.tif"): GdalFiles =
    viewshed(options, band, driver, name, creationOptions.asList())
suspend fun Dataset.viewshedTo(path: String, options: ViewshedOptions, vararg creationOptions: String, band: Int = 1, driver: String = "GTiff") =
    viewshedTo(path, options, band, driver, creationOptions.asList())

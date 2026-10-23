@file:OptIn(kotlinx.cinterop.ExperimentalForeignApi::class)
package dev.gdal4k

import kotlinx.cinterop.toKString

internal actual fun driverProfileFixtureEnvironment(name: String): String? =
    platform.posix.getenv(name)?.toKString()

package dev.gdal4k

internal actual fun driverProfileFixtureEnvironment(name: String): String? = System.getenv(name)

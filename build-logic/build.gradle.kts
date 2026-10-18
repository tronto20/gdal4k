plugins { `java-gradle-plugin` }
java { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
gradlePlugin {
    listOf("Cmake", "Ninja", "Emscripten", "Vcpkg").forEach { tool ->
        plugins.create(tool.lowercase()) {
            id = "dev.tronto.buildtools.${tool.lowercase()}"
            implementationClass = "dev.tronto.buildtools.${tool}Plugin"
        }
    }
}
val testProvisioning by tasks.registering(Exec::class) {
    group = "verification"
    description = "Test installers and Gradle plugin configuration without downloading toolchains"
    val python = providers.gradleProperty("buildTools.python").orElse(if (System.getProperty("os.name").startsWith("Windows")) "python" else "python3")
    commandLine(python.get(), "-m", "unittest", "discover", "-s", "src/test/python", "-v")
    environment("BUILD_TOOLS_PROJECT", projectDir.absolutePath)
    environment("GRADLE_EXECUTABLE", gradle.gradleHomeDir!!.resolve("bin/gradle${if (System.getProperty("os.name").startsWith("Windows")) ".bat" else ""}").absolutePath)
    environment("TEST_GRADLE_USER_HOME", gradle.gradleUserHomeDir.absolutePath)
    environment("JAVA_HOME", System.getProperty("java.home"))
    dependsOn(tasks.jar)
}
tasks.check { dependsOn(testProvisioning) }

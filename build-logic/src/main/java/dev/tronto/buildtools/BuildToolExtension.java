package dev.tronto.buildtools;

import org.gradle.api.file.DirectoryProperty;
import org.gradle.api.file.RegularFile;
import org.gradle.api.provider.Property;
import org.gradle.api.provider.Provider;

/** A versioned tool store shared through Gradle User Home unless explicitly overridden. */
public abstract class BuildToolExtension {
    public abstract Property<String> getVersion();
    public abstract DirectoryProperty getDirectory();
    /** Explicit existing installation; validated and never modified by the installer. */
    public abstract DirectoryProperty getLocalDirectory();
    public abstract DirectoryProperty getInstallationDirectory();
    public abstract Property<String> getDownloadUrl();
    public abstract Property<String> getSha256();
    public abstract Property<String> getRevision();
    /** vcpkg source revision and executable release are pinned independently. */
    public abstract Property<String> getBinaryVersion();
    public abstract Property<String> getBinaryDownloadUrl();
    public abstract Property<String> getBinarySha256();
    public abstract Property<String> getPythonExecutable();
    public abstract Property<String> getGitExecutable();
    public abstract Property<String> getHost();
    public abstract Property<String> getExecutablePath();
    public abstract Property<String> getBinPath();
    public Provider<RegularFile> getExecutable() {
        return getInstallationDirectory().file(getExecutablePath());
    }
}

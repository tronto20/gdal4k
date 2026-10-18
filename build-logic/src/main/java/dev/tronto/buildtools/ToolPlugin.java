package dev.tronto.buildtools;

import org.gradle.api.Plugin;
import org.gradle.api.Project;
import org.gradle.api.GradleException;
import org.gradle.api.provider.Provider;
import java.io.IOException;
import java.util.Locale;
import java.util.Properties;

abstract class ToolPlugin implements Plugin<Project> {
    private final String tool;
    ToolPlugin(String tool) { this.tool = tool; }

    @Override public void apply(Project project) {
        if (project != project.getRootProject()) throw new GradleException("Build tool plugins must be applied to the root project");
        Properties pins = new Properties();
        try (var source = ToolPlugin.class.getResourceAsStream("/dev/tronto/buildtools/versions.properties")) {
            if (source == null) throw new IOException("Missing tool pins");
            pins.load(source);
        } catch (IOException e) { throw new GradleException("Cannot load build tool pins", e); }
        String host = host();
        String os = host.substring(0, host.indexOf('-'));
        String defaultVersion = pins.getProperty(tool + ".version");
        String archive = pins.getProperty(tool + "." + host + ".archive", "");
        String executable = tool.equals("cmake") && os.equals("macos") ? "CMake.app/Contents/bin/cmake"
            : tool.equals("emscripten") ? "upstream/emscripten/emcc.py"
            : (tool.equals("cmake") ? "bin/" : "") + tool + (os.equals("windows") ? ".exe" : "");
        String prefix = "buildTools." + tool + ".";
        BuildToolExtension extension = project.getExtensions().create(tool + "Tool", BuildToolExtension.class);
        extension.getHost().convention(host);
        extension.getVersion().convention(project.getProviders().gradleProperty(prefix + "version").orElse(defaultVersion));
        var rootDirectory = project.getLayout().getProjectDirectory();
        String rootPath = rootDirectory.getAsFile().getAbsolutePath();
        var defaultStore = new java.io.File(project.getGradle().getGradleUserHomeDir(), "build-tools/" +
            (tool.equals("emscripten") ? "emsdk" : tool));
        extension.getDirectory().convention(project.getLayout().dir(project.getProviders().gradleProperty(prefix + "directory")
            .map(path -> resolve(rootPath, path)).orElse(defaultStore)));
        extension.getLocalDirectory().convention(project.getLayout().dir(project.getProviders().gradleProperty(prefix + "localDirectory")
            .map(path -> resolve(rootPath, path))));
        extension.getInstallationDirectory().convention(extension.getLocalDirectory().orElse(
            extension.getDirectory().dir(extension.getVersion().map(v -> v + "/" + host))));
        Provider<String> executablePath = tool.equals("cmake") && os.equals("macos")
            ? extension.getLocalDirectory().map(dir -> dir.file(executable).getAsFile().isFile() ? executable : "bin/cmake").orElse(executable)
            : project.getProviders().provider(() -> executable);
        extension.getExecutablePath().convention(project.getProviders().gradleProperty(prefix + "executablePath").orElse(executablePath));
        extension.getBinPath().convention(extension.getExecutablePath().map(path -> {
            int slash = path.replace('\\', '/').lastIndexOf('/');
            return slash < 0 ? "." : path.substring(0, slash);
        }));
        extension.getPythonExecutable().convention(project.getProviders().gradleProperty("buildTools.python").orElse(os.equals("windows") ? "python" : "python3"));
        extension.getGitExecutable().convention(project.getProviders().gradleProperty("buildTools.git").orElse("git"));
        extension.getRevision().convention(project.getProviders().gradleProperty(prefix + "revision")
            .orElse(extension.getVersion().map(v -> v.equals(defaultVersion) ? pins.getProperty(tool + ".revision", "") : "")));
        Provider<String> url = extension.getVersion().map(v -> switch (tool) {
            case "cmake" -> "https://github.com/Kitware/CMake/releases/download/v" + v + "/cmake-" + v + "-" + archive;
            case "ninja" -> "https://github.com/ninja-build/ninja/releases/download/v" + v + "/" + archive;
            case "emscripten" -> "https://github.com/emscripten-core/emsdk.git";
            default -> "https://github.com/microsoft/vcpkg.git";
        });
        extension.getDownloadUrl().convention(project.getProviders().gradleProperty(prefix + "url").orElse(url));
        extension.getSha256().convention(project.getProviders().gradleProperty(prefix + "sha256")
            .orElse(extension.getVersion().map(v -> v.equals(defaultVersion) ? pins.getProperty(tool + "." + host + ".sha256", "") : "")));
        extension.getBinaryVersion().convention(project.getProviders().gradleProperty(prefix + "binaryVersion")
            .orElse(extension.getVersion().map(v -> v.equals(defaultVersion) ? pins.getProperty(tool + ".binaryVersion", "") : "")));
        extension.getBinaryDownloadUrl().convention(project.getProviders().gradleProperty(prefix + "binaryUrl")
            .orElse(extension.getBinaryVersion().map(v -> tool.equals("vcpkg") && !v.isEmpty()
                ? "https://github.com/microsoft/vcpkg-tool/releases/download/" + v + "/" + pins.getProperty(tool + "." + host + ".binaryAsset", "") : "")));
        extension.getBinarySha256().convention(project.getProviders().gradleProperty(prefix + "binarySha256")
            .orElse(extension.getVersion().map(v -> v.equals(defaultVersion) ? pins.getProperty(tool + "." + host + ".binarySha256", "") : "")));
        String taskName = "prepare" + Character.toUpperCase(tool.charAt(0)) + tool.substring(1);
        project.getTasks().register(taskName, PrepareTool.class, task -> {
            task.setGroup("build tools");
            task.setDescription("Prepare the pinned " + tool + " tool in its configurable local store");
            task.getTool().set(tool);
            task.getVersion().set(extension.getVersion());
            task.getHost().set(extension.getHost());
            task.getDirectory().set(extension.getDirectory());
            task.getInstallationDirectory().set(extension.getInstallationDirectory());
            task.getDownloadUrl().set(extension.getDownloadUrl());
            task.getSha256().set(extension.getSha256());
            task.getRevision().set(extension.getRevision());
            task.getBinaryVersion().set(extension.getBinaryVersion());
            task.getBinaryDownloadUrl().set(extension.getBinaryDownloadUrl());
            task.getBinarySha256().set(extension.getBinarySha256());
            task.getExecutablePath().set(extension.getExecutablePath());
            task.getPythonExecutable().set(extension.getPythonExecutable());
            task.getGitExecutable().set(extension.getGitExecutable());
            task.getOffline().set(project.getGradle().getStartParameter().isOffline());
            task.getUseLocal().set(extension.getLocalDirectory().map(ignored -> true).orElse(false));
        });
    }
    private static java.io.File resolve(String root, String value) {
        var file = new java.io.File(value);
        return file.isAbsolute() ? file : new java.io.File(root, value);
    }
    private static String host() {
        String osName = System.getProperty("os.name").toLowerCase(Locale.ROOT);
        String os = osName.contains("windows") ? "windows" : osName.contains("mac") ? "macos" : osName.contains("linux") ? "linux" : "unsupported";
        String archName = System.getProperty("os.arch").toLowerCase(Locale.ROOT);
        String arch = switch (archName) { case "aarch64", "arm64" -> "arm64"; case "amd64", "x86_64" -> "x64"; default -> "unsupported"; };
        if (os.equals("unsupported") || arch.equals("unsupported")) throw new GradleException("Unsupported build tool host: " + osName + "/" + archName);
        return os + "-" + arch;
    }
}

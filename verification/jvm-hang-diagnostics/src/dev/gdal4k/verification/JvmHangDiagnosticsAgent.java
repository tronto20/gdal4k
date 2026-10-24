package dev.gdal4k.verification;

import java.io.BufferedWriter;
import java.lang.management.LockInfo;
import java.lang.management.ManagementFactory;
import java.lang.management.MonitorInfo;
import java.lang.management.ThreadInfo;
import java.lang.management.ThreadMXBean;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.Base64;

/** Test-worker diagnostics only: no transformer, attach entry point or shutdown hook. */
public final class JvmHangDiagnosticsAgent {
    private static final long PID = ProcessHandle.current().pid();
    private static final DateTimeFormatter FILE_TIME =
            DateTimeFormatter.ofPattern("uuuuMMdd'T'HHmmss.SSS'Z'").withZone(ZoneOffset.UTC);

    private JvmHangDiagnosticsAgent() {}

    public static void premain(String arguments) {
        marker("PREMAIN_START", "");
        try {
            Options options = options(arguments);
            Path workerDirectory = options.output().resolve("worker-" + PID);
            Files.createDirectories(workerDirectory);
            Thread collector = new Thread(() -> collect(workerDirectory, options.intervalMillis()),
                    "gdal4k-test-hang-diagnostics");
            collector.setDaemon(true);
            collector.start();
            marker("PREMAIN_COMPLETE", "intervalMillis=" + options.intervalMillis()
                    + " directory=" + workerDirectory);
        } catch (Throwable error) {
            failure("premain", error);
        }
    }

    private record Options(Path output, long intervalMillis) {}

    private static Options options(String arguments) {
        Path output = Path.of("build", "test-results", "jvm-hang-diagnostics").toAbsolutePath();
        long interval = 60_000;
        boolean outputSeen = false;
        boolean intervalSeen = false;
        if (arguments != null && !arguments.isEmpty()) {
            for (String option : arguments.split(";")) {
                String[] pair = option.split("=", 2);
                if (pair.length != 2) throw new IllegalArgumentException("Malformed diagnostic option");
                if (pair[0].equals("output64") && !outputSeen) {
                    output = Path.of(new String(Base64.getUrlDecoder().decode(pair[1]), StandardCharsets.UTF_8));
                    outputSeen = true;
                } else if (pair[0].equals("intervalMillis") && !intervalSeen) {
                    interval = Long.parseLong(pair[1]);
                    intervalSeen = true;
                } else {
                    throw new IllegalArgumentException("Unknown or duplicate diagnostic option: " + pair[0]);
                }
            }
        }
        if (interval < 10) throw new IllegalArgumentException("Diagnostic interval must be at least 10 ms");
        return new Options(output.toAbsolutePath(), interval);
    }

    private static void collect(Path directory, long intervalMillis) {
        marker("DAEMON_STARTED", "threadId=" + Thread.currentThread().threadId());
        long sequence = 0;
        while (true) {
            try {
                dump(directory, ++sequence);
            } catch (Throwable error) {
                failure("snapshot", error);
            }
            try {
                Thread.sleep(intervalMillis);
            } catch (InterruptedException interrupted) {
                marker("DAEMON_INTERRUPTED", "");
                Thread.currentThread().interrupt();
                return;
            }
        }
    }

    private static void dump(Path directory, long sequence) throws Exception {
        ThreadMXBean bean = ManagementFactory.getThreadMXBean();
        boolean monitors = bean.isObjectMonitorUsageSupported();
        boolean synchronizers = bean.isSynchronizerUsageSupported();
        ThreadInfo[] threads = bean.dumpAllThreads(monitors, synchronizers, Integer.MAX_VALUE);
        Instant now = Instant.now();
        Path output = directory.resolve("thread-dump-" + FILE_TIME.format(now) + "-" + sequence + ".txt");
        try (BufferedWriter writer = Files.newBufferedWriter(output, StandardCharsets.UTF_8,
                StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)) {
            line(writer, "GDAL4K_JVM_THREAD_SNAPSHOT workerPid=" + PID + " utc=" + now
                    + " sequence=" + sequence + " platformThreads=" + threads.length);
            line(writer, "monitorUsageSupported=" + monitors + " synchronizerUsageSupported=" + synchronizers
                    + " contentionMonitoringEnabled=" + bean.isThreadContentionMonitoringEnabled());
            writer.flush();
            for (ThreadInfo info : threads) {
                if (info == null) continue;
                line(writer, "THREAD id=" + info.getThreadId() + " name=\"" + escaped(info.getThreadName())
                        + "\" state=" + info.getThreadState() + " inNative=" + info.isInNative()
                        + " daemon=" + info.isDaemon() + " suspended=" + info.isSuspended());
                line(writer, "  blockedCount=" + info.getBlockedCount() + " waitedCount=" + info.getWaitedCount());
                line(writer, "  lockName=" + info.getLockName() + " lockOwnerId=" + info.getLockOwnerId()
                        + " lockOwnerName=" + escaped(info.getLockOwnerName()));
                StackTraceElement[] stack = info.getStackTrace();
                line(writer, "  STACK frames=" + stack.length);
                for (StackTraceElement frame : stack) line(writer, "    at " + frame);
                MonitorInfo[] lockedMonitors = info.getLockedMonitors();
                line(writer, "  LOCKED_MONITORS count=" + lockedMonitors.length);
                for (MonitorInfo monitor : lockedMonitors) {
                    line(writer, "    " + monitor + " stackDepth=" + monitor.getLockedStackDepth()
                            + " frame=" + monitor.getLockedStackFrame());
                }
                LockInfo[] lockedSynchronizers = info.getLockedSynchronizers();
                line(writer, "  LOCKED_SYNCHRONIZERS count=" + lockedSynchronizers.length);
                for (LockInfo synchronizer : lockedSynchronizers) line(writer, "    " + synchronizer);
                writer.flush();
            }
            line(writer, "END_SNAPSHOT workerPid=" + PID + " sequence=" + sequence);
        }
        marker("SNAPSHOT_COMPLETE", "sequence=" + sequence + " file=" + output);
    }

    private static String escaped(String value) {
        return value == null ? "null" : value.replace("\\", "\\\\")
                .replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t");
    }

    private static void line(BufferedWriter writer, String value) throws Exception {
        writer.write(value);
        writer.newLine();
    }

    private static void marker(String event, String details) {
        System.out.println("GDAL4K_JVM_HANG_AGENT " + event + " workerPid=" + PID
                + " utc=" + Instant.now() + (details.isEmpty() ? "" : " " + details));
        System.out.flush();
    }

    private static void failure(String phase, Throwable error) {
        marker("ERROR", "phase=" + phase + " type=" + error.getClass().getName()
                + " message=" + escaped(error.getMessage()));
        error.printStackTrace(System.out);
        System.out.flush();
    }
}

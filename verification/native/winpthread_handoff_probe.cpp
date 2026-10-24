// Diagnostic conformance probe for the exact pinned winpthread cond.o.
// Its only altered relocation names probe_unlock_hook; production SDK bytes stay intact.
#include <windows.h>
#include <pthread.h>
#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <thread>
#include <vector>

static_assert(sizeof(void*) == 8 && sizeof(CRITICAL_SECTION) == 40, "Pinned Windows x64 ABI");

struct Protocol {
    std::mutex mutex;
    std::condition_variable ready_cv, resume_cv;
    bool shared = false, cancel = false, ready = false, finished = false, stop = false;
    int count = 0;
    std::vector<int> rows, values;
    std::condition_variable& resume() { return shared ? ready_cv : resume_cv; }
};
static Protocol* protocol;
static pthread_mutex_t* target_mutex;
static std::atomic<DWORD> producer_tid{0}, consumer_tid{0};
static std::atomic<bool> park_once{true}, hook_error{false};
static std::atomic<int> consumer_unlocks{0}, before_second{-999}, before_third{-999};
static HANDLE first_wait, producer_parked, release_producer, second_wait, third_wait;
static HANDLE producer_progress, producer_done, consumer_done;

// The copied object's exact instruction pin establishes this private layout.
// Read the queue count under the same Win32 critical section its instructions use.
static int queue_count(std::condition_variable& cv) {
    intptr_t pointer = 0;
    std::memcpy(&pointer, cv.native_handle(), sizeof(pointer));
    if (pointer <= 0) return -999;
    auto memory = reinterpret_cast<unsigned char*>(pointer);
    unsigned magic = 0;
    std::memcpy(&magic, memory, sizeof(magic));
    if (magic != 0xc0bab1fd) { hook_error = true; return -999; }
    auto lock = reinterpret_cast<CRITICAL_SECTION*>(memory + 0x40);
    EnterCriticalSection(lock);
    int value = 0; std::memcpy(&value, memory + 0x68, sizeof(value));
    LeaveCriticalSection(lock);
    return value;
}

extern "C" int probe_unlock_hook(pthread_mutex_t* mutex) {
    const int result = pthread_mutex_unlock(mutex); // The original SDK mutex implementation.
    if (result || mutex != target_mutex) return result;
    const DWORD tid = GetCurrentThreadId();
    if (tid == producer_tid && park_once.exchange(false)) {
        SetEvent(producer_parked);
        if (WaitForSingleObject(release_producer, 10000) != WAIT_OBJECT_0) hook_error = true;
    } else if (tid == consumer_tid) {
        const int number = ++consumer_unlocks;
        if (number == 1) SetEvent(first_wait);
        if (number == 2) { before_second = queue_count(protocol->ready_cv); SetEvent(second_wait); }
        if (number == 3) { before_third = queue_count(protocol->ready_cv); SetEvent(third_wait); }
    }
    return result;
}

static bool wait(HANDLE event, DWORD milliseconds = 5000) {
    return WaitForSingleObject(event, milliseconds) == WAIT_OBJECT_0;
}
static HANDLE event() { return CreateEventW(nullptr, TRUE, FALSE, nullptr); }
static void producer() {
    producer_tid = GetCurrentThreadId();
    try {
        std::unique_lock<std::mutex> lock(protocol->mutex);
        protocol->count = 3;
        protocol->values = {0, 1, 2};
        protocol->ready = true;
        protocol->ready_cv.notify_one();
        while (protocol->count > 0 && !protocol->stop) protocol->resume().wait(lock);
        if (!protocol->stop) {
            SetEvent(producer_progress);
            protocol->values.push_back(3);
            protocol->count = 1;
            protocol->finished = protocol->ready = true;
            protocol->ready_cv.notify_one();
        }
    } catch (...) { hook_error = true; }
    SetEvent(producer_done);
}
static void consumer() {
    consumer_tid = GetCurrentThreadId();
    try {
        std::unique_lock<std::mutex> lock(protocol->mutex);
        while (!protocol->ready && !protocol->stop) protocol->ready_cv.wait(lock);
        if (!protocol->stop) {
            if (protocol->count != 3) hook_error = true;
            protocol->rows.push_back(protocol->count);
            if (protocol->cancel) {
                protocol->stop = true; protocol->count = -1;
                protocol->resume().notify_one();
            } else {
                protocol->count = 0; protocol->ready = false;
                protocol->resume().notify_one();
                while (!protocol->ready && !protocol->stop) protocol->ready_cv.wait(lock);
                if (!protocol->stop) {
                    if (protocol->count != 1 || !protocol->finished ||
                        protocol->values != std::vector<int>({0, 1, 2, 3})) hook_error = true;
                    protocol->rows.push_back(protocol->count);
                    protocol->count = 0;
                    protocol->rows.push_back(0); // Finished producer: EOF.
                    protocol->rows.push_back(0); // Repeated EOF.
                }
            }
        }
    } catch (...) { hook_error = true; }
    SetEvent(consumer_done);
}

static void finish(const char* mode, const char* result, int code, bool old_stall,
                   bool joined, int blocked_count) {
    std::printf("WINPTHREAD_HANDOFF {\"mode\":\"%s\",\"result\":\"%s\",\"pid\":%lu,"
                "\"producerTid\":%lu,\"consumerTid\":%lu,\"consumerUnlocks\":%d,"
                "\"queueBeforeSecondWait\":%d,\"queueBeforeThirdWait\":%d,"
                "\"observedBlockedQueueCount\":%d,\"oldStallObserved\":%s,"
                "\"hookError\":%s,\"threadsJoined\":%s,\"rowsObservedAfterJoin\":%s,\"rows\":[",
                mode, result, static_cast<unsigned long>(GetCurrentProcessId()),
                static_cast<unsigned long>(producer_tid.load()), static_cast<unsigned long>(consumer_tid.load()),
                consumer_unlocks.load(), before_second.load(), before_third.load(), blocked_count,
                old_stall ? "true" : "false", hook_error ? "true" : "false",
                joined ? "true" : "false", joined ? "true" : "false");
    // A failed schedule can still have a live consumer. Never read its vector
    // until both threads have joined; the supervisor retains the failure.
    if (joined) for (size_t i = 0; i < protocol->rows.size(); ++i)
        std::printf("%s%d", i ? "," : "", protocol->rows[i]);
    std::printf("]}\n"); std::fflush(stdout);
    // Only error paths can have joinable threads. Exit the owned probe itself;
    // the Python supervisor also retains an independent Job Object deadline.
    ExitProcess(static_cast<UINT>(code));
}

int main(int argc, char** argv) {
    if (argc != 2 || (std::strcmp(argv[1], "shared") && std::strcmp(argv[1], "split") &&
                      std::strcmp(argv[1], "split-cancel"))) return 2;
    const char* mode = argv[1];
    Protocol data; protocol = &data;
    data.shared = !std::strcmp(mode, "shared"); data.cancel = !std::strcmp(mode, "split-cancel");
    target_mutex = data.mutex.native_handle();
    first_wait = event(); producer_parked = event(); release_producer = event();
    second_wait = event(); third_wait = event(); producer_progress = event();
    producer_done = event(); consumer_done = event();
    if (!first_wait || !producer_parked || !release_producer || !second_wait || !third_wait ||
        !producer_progress || !producer_done || !consumer_done) return 3;
    std::thread caller(consumer);
    if (!wait(first_wait)) finish(mode, "consumer-startup-failed", 4, false, false, -999);
    std::thread worker(producer);
    if (!wait(producer_parked)) finish(mode, "producer-window-not-observed", 5, false, false, -999);
    bool observed = false;
    int blocked_count = -999;
    if (data.shared) {
        if (!wait(third_wait)) finish(mode, "mechanism-not-reproduced", 6, false, false, -999);
        if (before_second != 1 || before_third != 0 || wait(producer_progress, 0))
            finish(mode, "token-window-not-proved", 7, false, false, -999);
        SetEvent(release_producer);
        const ULONGLONG end = GetTickCount64() + 1000;
        do {
            blocked_count = queue_count(data.ready_cv);
            if (blocked_count == -2) break;
            Sleep(1);
        } while (GetTickCount64() < end);
        {
            std::lock_guard<std::mutex> lock(data.mutex);
            observed = blocked_count == -2 && data.count == 0 && !data.ready && !data.finished &&
                       !wait(producer_progress, 0) && !wait(consumer_done, 0);
            data.stop = true; data.count = -1;
        }
        data.ready_cv.notify_all(); // Bounded cancellation/cleanup, not a fake progress result.
        if (!wait(producer_done, 2000) || !wait(consumer_done, 2000))
            finish(mode, "old-stall-cleanup-failed", 8, observed, false, blocked_count);
        worker.join(); caller.join();
        finish(mode, observed && !hook_error ? "expected-stall-observed-and-cancelled" : "mechanism-not-reproduced",
               observed && !hook_error ? 0 : 9, observed, true, blocked_count);
    }
    if (data.cancel) {
        if (!wait(consumer_done)) finish(mode, "cancel-not-observed", 10, false, false, -999);
    } else {
        if (!wait(second_wait)) finish(mode, "second-read-not-observed", 11, false, false, -999);
        if (before_second != 0 || queue_count(data.resume_cv) != 1)
            finish(mode, "separate-queue-window-not-proved", 12, false, false, -999);
    }
    SetEvent(release_producer);
    if (!wait(producer_done) || !wait(consumer_done)) finish(mode, "separate-queue-progress-failed", 13, false, false, -999);
    worker.join(); caller.join();
    const bool correct = data.cancel ? (data.rows == std::vector<int>({3}) && data.stop)
                                     : (data.rows == std::vector<int>({3, 1, 0, 0}) && data.finished);
    finish(mode, correct && !hook_error ? (data.cancel ? "cancel-completed" : "progress-and-eof-completed") : "wrong-data",
           correct && !hook_error ? 0 : 14, false, true, -999);
}

#include <hip/hip_runtime.h>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

static void check(hipError_t rc, const char* expr) {
    if (rc != hipSuccess) throw std::runtime_error(std::string(expr) + ": " + hipGetErrorString(rc));
}
#define HIP(expr) check((expr), #expr)
constexpr size_t words = 1 << 20;
constexpr size_t bytes = words * sizeof(unsigned);
__global__ void fill(unsigned* p, unsigned seed) {
    size_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < words) p[i] = unsigned(i) ^ seed;
}
static void verify(const std::vector<unsigned>& data, unsigned seed) {
    for (size_t i = 0; i < words; ++i)
        if (data[i] != (unsigned(i) ^ seed))
            throw std::runtime_error("Data mismatch at word " + std::to_string(i) +
                                     " actual=" + std::to_string(data[i]) +
                                     " expected=" + std::to_string(unsigned(i) ^ seed));
}
static unsigned seed(int gpu) { return 0x9e3779b9u * unsigned(gpu + 1); }

int main(int argc, char** argv) {
    try {
        int count = 0;
        HIP(hipGetDeviceCount(&count));
        std::printf("HIP devices: %d\n", count);
        std::fflush(stdout);
        if (count < 8) throw std::runtime_error("Expected at least eight logical GPUs");
        std::vector<unsigned*> src(count), dst(count);
        std::vector<std::string> bdfs(count);
        std::vector<unsigned> host(words);
        for (int g = 0; g < count; ++g) {
            HIP(hipSetDevice(g));
            hipDeviceProp_t prop{};
            char bdf[32]{};
            HIP(hipGetDeviceProperties(&prop, g));
            HIP(hipDeviceGetPCIBusId(bdf, sizeof(bdf), g));
            bdfs[g] = bdf;
            HIP(hipMalloc(&src[g], bytes));
            HIP(hipMalloc(&dst[g], bytes));
            for (size_t i = 0; i < words; ++i) host[i] = unsigned(i) ^ seed(g);
            HIP(hipMemcpy(dst[g], host.data(), bytes, hipMemcpyHostToDevice));
            std::fill(host.begin(), host.end(), 0);
            HIP(hipMemcpy(host.data(), dst[g], bytes, hipMemcpyDeviceToHost));
            verify(host, seed(g));
            hipLaunchKernelGGL(fill, dim3(words / 256), dim3(256), 0, 0, src[g], seed(g));
            HIP(hipGetLastError()); HIP(hipDeviceSynchronize());
            HIP(hipMemcpy(host.data(), src[g], bytes, hipMemcpyDeviceToHost));
            verify(host, seed(g));
            std::printf("LOCAL PASS device=%d bdf=%s memory=%zu CUs=%d\n", g, bdf, prop.totalGlobalMem, prop.multiProcessorCount);
            std::fflush(stdout);
        }
        std::string mode = argc > 1 ? argv[1] : "local";
        if (mode == "peer" || mode == "stress" || mode == "isolated") {
            auto target = [&](int g) { return bdfs[g].rfind("0000:f6:00.", 0) == 0; };
            if (mode == "isolated") {
                int targets = 0;
                for (int g = 0; g < count; ++g) targets += target(g);
                if (count != 15 || targets != 8)
                    throw std::runtime_error("Isolated test requires GPU7's eight partitions and seven other GPUs");
            }
            int passed = 0, skipped = 0, excluded = 0;
            for (int d = 0; d < count; ++d) for (int s = 0; s < count; ++s) {
                if (d == s) continue;
                if (mode == "isolated" && target(d) != target(s)) { ++excluded; continue; }
                int can = 0;
                HIP(hipDeviceCanAccessPeer(&can, d, s));
                if (!can) { ++skipped; continue; }
                HIP(hipSetDevice(d));
                auto rc = hipDeviceEnablePeerAccess(s, 0);
                if (rc != hipErrorPeerAccessAlreadyEnabled) HIP(rc);
                HIP(hipMemset(dst[d], 0, bytes));
                HIP(hipMemcpyPeer(dst[d], d, src[s], s, bytes));
                HIP(hipDeviceSynchronize());
                HIP(hipMemcpy(host.data(), dst[d], bytes, hipMemcpyDeviceToHost));
                try { verify(host, seed(s)); }
                catch (const std::exception& e) {
                    throw std::runtime_error("peer src=" + std::to_string(s) +
                                             " dst=" + std::to_string(d) + " " + e.what());
                }
                ++passed;
            }
            std::printf("PEER PASS directed_pairs=%d unsupported_pairs=%d excluded_cross_group_pairs=%d bytes_per_pair=%zu\n", passed, skipped, excluded, bytes);
            std::fflush(stdout);
            if (skipped) throw std::runtime_error("Peer coverage incomplete");
        }
        if (mode == "stress" || mode == "isolated") {
            int seconds = argc > 2 ? std::atoi(argv[2]) : 15;
            if (seconds <= 0 || seconds > 60) throw std::runtime_error("Duration must be 1..60 seconds");
            std::atomic<int> failures{0};
            std::mutex print_lock;
            std::vector<std::thread> workers;
            for (int g = 0; g < count; ++g) workers.emplace_back([&, g] {
                try {
                    HIP(hipSetDevice(g));
                    auto end = std::chrono::steady_clock::now() + std::chrono::seconds(seconds);
                    unsigned rounds = 0;
                    do {
                        for (int k = 0; k < 32; ++k)
                            hipLaunchKernelGGL(fill, dim3(words / 256), dim3(256), 0, 0, src[g], seed(g) ^ rounds);
                        HIP(hipGetLastError()); HIP(hipDeviceSynchronize());
                        ++rounds;
                    } while (std::chrono::steady_clock::now() < end);
                    std::vector<unsigned> result(words);
                    HIP(hipMemcpy(result.data(), src[g], bytes, hipMemcpyDeviceToHost));
                    verify(result, seed(g) ^ (rounds - 1));
                    std::lock_guard<std::mutex> guard(print_lock);
                    std::printf("CONCURRENT PASS device=%d rounds=%u\n", g, rounds);
                } catch (const std::exception& e) {
                    ++failures;
                    std::lock_guard<std::mutex> guard(print_lock);
                    std::fprintf(stderr, "CONCURRENT FAIL device=%d %s\n", g, e.what());
                }
            });
            for (auto& t : workers) t.join();
            if (failures) throw std::runtime_error("Concurrent correctness failure");
        }
        for (int g = 0; g < count; ++g) {
            HIP(hipSetDevice(g)); HIP(hipFree(src[g])); HIP(hipFree(dst[g]));
        }
        std::puts("ALL CHECKS PASSED");
        return 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "FAIL: %s\n", e.what());
        return 1;
    }
}

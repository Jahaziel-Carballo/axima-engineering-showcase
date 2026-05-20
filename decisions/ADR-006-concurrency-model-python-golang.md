# ADR-006: Concurrency model for GPU orchestration — Python multiprocessing + threading vs. Go goroutines

- **Status:** Accepted
- **Date:** 2025-04-15
- **Deciders:** Jahaziel Carballo (Solo Developer)

---

## Context

Axima's GPU worker must simultaneously:

- Pull messages from Pub/Sub (I/O-bound, streaming pull).
- Orchestrate multiple child processes performing GPU inference (CPU/GPU-bound, bypassing Python's GIL).
- Run a fairness-aware dispatcher with its own event loop.
- Execute async uploads to Cloud Storage and Firestore (I/O-bound).
- Serve a health-check HTTP endpoint (FastAPI + uvicorn).
- Handle SIGTERM for graceful shutdown within 25 seconds.

Python offers three concurrency primitives: `multiprocessing`, `threading`, and `asyncio`. Each solves a different class of problem. The challenge was to select and combine the right primitives for each workload while keeping the system maintainable and debuggable.

Additionally, I wanted to validate that my concurrency design is not tied to Python quirks but is grounded in universal patterns that translate cleanly to Go, a language I am actively learning.

---

## Decision

**Use a hybrid concurrency model in Python, with each primitive applied to its ideal workload. The same logical architecture maps naturally to Go's goroutine + channel model, as detailed below.**

### Python implementation (current)

| Workload | Primitive used | Reasoning |
|----------|---------------|-----------|
| GPU inference (PaddleOCR) | `multiprocessing.Process` (3 child processes) | GPU kernels run in C++ but Python's GIL blocks CPU-bound pre/post-processing. `multiprocessing` bypasses the GIL entirely. |
| IPC between parent and child | `/dev/shm` with atomic file writes (tmpfs) | Avoids `multiprocessing.Manager` serialization overhead (see ADR-001). |
| Pub/Sub streaming pull | `google-cloud-pubsub` subscriber (runs in main thread with background I/O threads) | The Pub/Sub client library internally uses threads for gRPC streaming. |
| Fairness dispatcher | Dedicated `threading.Thread` | The dispatcher is mostly I/O-bound (queue operations) but needs shared access to `tenant_buffers` and `child_queues`. Threads share memory safely with the right locks. |
| Async uploads | `ThreadPoolExecutor` (20 workers) + `BoundedSemaphore` (50) | Uploads are I/O-bound network calls. Threads are ideal and avoid the complexity of `asyncio` for this isolated task. |
| HTTP health endpoint | FastAPI with `uvicorn` (1 worker, async) | `asyncio` is perfect for HTTP request handling, especially for lightweight endpoints like `/health`. |
| Graceful shutdown | `threading.Timer` (25s hard timeout) + signal handler | Ensures the process exits even if child processes hang. |

**Why not asyncio everywhere?**
- GPU inference blocks; `asyncio` cannot offload it without a thread or process pool anyway.
- `multiprocessing` is the only way to use multiple CPU cores for pre/post-processing in Python.
- Mixing `asyncio` with synchronous threads is possible but adds complexity for no real gain in this use case.

**Why not a single multiprocessing pool?**
- Different concurrency needs: some tasks are CPU-bound (inference), some I/O-bound (uploads, Pub/Sub). A uniform pool would misallocate resources.

### How this maps to Go (if reimplemented)

| Python component | Go equivalent | Key difference |
|-----------------|--------------|----------------|
| `multiprocessing.Process` (child workers) | `go func()` (goroutines) with a worker pool pattern | Go has no GIL; goroutines can do CPU work natively. GPU calls would still be via CGo, but goroutines handle it more lightly than Python processes. |
| IPC via `/dev/shm` | Go channels (`chan`) or `bytes.Buffer` shared between goroutines | Channels are typed, compile-time safe, and have built-in backpressure. No manual file I/O needed. |
| `threading.Thread` (dispatcher) | Goroutine with `select` over channels | Goroutines are lighter than threads (2 KB stack vs. 1 MB), so a dedicated dispatcher goroutine is trivial. |
| `ThreadPoolExecutor` (async uploads) | Goroutine pool with a buffered channel as semaphore | Go's `sync.Semaphore` or a buffered channel pattern provides the same bounded concurrency. |
| `asyncio` + FastAPI (health HTTP) | `net/http` package (already concurrent) | Go's standard HTTP server is non-blocking by default; no separate framework needed for simple health checks. |
| `threading.Timer` (hard shutdown) | `context.WithTimeout` or `time.After` | Go's context package elegantly propagates cancellation across all goroutines. Graceful shutdown in Go is idiomatic. |

### Why I stayed with Python (for now)

- **PaddleOCR and PaddleX are Python-only** with C++ backends. Rewriting the inference engine in Go would require wrapping C++ libraries via CGo, which adds significant development and maintenance cost.
- The Python hybrid model works reliably in production, processing thousands of documents with zero data loss under Spot VM preemption.
- The concurrency patterns I've mastered — worker pools, bounded semaphores, event-driven dispatch, graceful shutdown sequences — are language-agnostic. Moving them to Go is a matter of syntax, not a conceptual leap.

---

## Alternatives considered

| Alternative | Rejected because |
|-------------|------------------|
| **Single-process Python with `asyncio` only** | Cannot bypass GIL for CPU/GPU work. Would starve I/O during inference. |
| **Python with `concurrent.futures.ProcessPoolExecutor` for everything** | Too heavy for lightweight tasks like uploads; high overhead for frequent small tasks. |
| **Go from the start** | PaddleOCR bindings are Python-only. Rewriting them would delay product launch by months. |
| **Rust for the inference engine** | Even higher learning curve than Go; smaller ML ecosystem than Python. |

---

## Consequences

### Positive

- **Optimal resource utilization:** Each concurrency primitive is used where it excels: processes for GPU/CPU, threads for I/O, asyncio for HTTP.
- **Clean separation of concerns:** The dispatcher, uploader, and HTTP server are independent components that communicate via well-defined queues and files.
- **Transferable knowledge:** Every pattern used here (worker pools, bounded channels, select-based dispatching, context-based cancellation) exists in Go's standard library or idiomatic community packages. I can implement the same architecture in Go without redesigning the system logic.
- **Interview-ready demonstration:** This ADR, combined with the code samples in this repository, proves I understand concurrency from first principles, not just Python-specific APIs.

### Negative

- **Multilingual complexity in one process:** Mixing `multiprocessing`, `threading`, and `asyncio` requires careful handling of fork safety, signal handling, and lock lifetimes. New contributors would need time to understand the interplay.
- **Python's multiprocessing startup cost:** `spawn` is required for CUDA compatibility but adds ~500ms per child process startup. Go's goroutines have near-zero startup time.
- **Debugging mixed concurrency:** Race conditions across processes + threads + asyncio are hard to reproduce. Extensive logging and the health-check endpoint mitigate this.

---

## Implications for a future Go migration

If Axima's inference engine were to become available with Go bindings (e.g., via ONNX Runtime or a future CGo wrapper), migrating the worker to Go would provide:

- **Simpler deployment:** A single statically compiled binary instead of a Python + CUDA + PaddlePaddle container.
- **Faster startup:** No Python interpreter warm-up; goroutines start instantly.
- **Lower memory overhead:** Goroutines use 2 KB of stack vs. Python's process overhead (~100 MB per child).
- **Stronger type safety:** Go's compiler catches concurrency mismatches at build time.

The logical architecture — worker pools, bounded channels, fairness dispatcher, graceful shutdown via context — would remain identical. Only the implementation syntax would change.

---

## References

- Python `multiprocessing` documentation: https://docs.python.org/3/library/multiprocessing.html
- Go concurrency patterns (Go Blog): https://go.dev/blog/pipelines
- "Concurrency is not parallelism" — Rob Pike: https://go.dev/talks/2012/waza.slide
- Python GIL and its impact on ML workloads
- Google Cloud Pub/Sub client library internals (gRPC threading model)
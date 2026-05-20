# ADR-001: IPC between child processes and parent via shared memory (tmpfs) instead of multiprocessing.Manager

- **Status:** Accepted
- **Date:** 2025-01-15
- **Deciders:** Jahaziel Carballo (Solo Developer)

---

## Context

Axima's GPU worker spawns multiple child processes (3 by default) that perform OCR and document structure extraction. Each child produces a large JSON result (often 2–15 MB) containing page blocks, tables, and metadata. This result must be communicated back to the parent process so it can be uploaded to Cloud Storage and Firestore.

Initially, I used Python's `multiprocessing.Manager().dict()` to pass results between processes. This approach failed under production load for three reasons:

1. **Serialization bottleneck:** Every write to a managed dict triggers pickling, which becomes prohibitively slow for large nested structures (the "Pickle Wall").
2. **Memory pressure:** Managed objects live in a separate manager process, doubling memory usage for large payloads.
3. **Reliability issues:** Under heavy GPU load, the manager process occasionally became unresponsive, causing the parent to hang waiting for results.

I needed an IPC mechanism that was fast, memory-efficient, and immune to serialization overhead.

---

## Decision

**Use the Linux tmpfs filesystem (`/dev/shm`) as a shared memory channel, with atomic file writes and sentinel files for signaling.**

### How it works

1. The child process writes its result as a JSON file directly to `/dev/shm/{trace_id}.res.json.tmp`.
2. After the write is complete and flushed, the child atomically renames the `.tmp` file to its final name (`{trace_id}.res.json`).
3. The child then creates an empty sentinel file (`{trace_id}.done`) to signal to the parent that the result is ready.
4. The parent polls for the sentinel file (non-blocking, with a configurable timeout) and reads the result once it appears.
5. Both the result file and the sentinel file are deleted immediately after the parent finishes reading.

Atomic rename (`os.rename`) is guaranteed to be atomic on Linux, preventing the parent from ever reading a partially written file.

### Why `/dev/shm`

- `/dev/shm` is a tmpfs mount backed by RAM, offering near-in-memory read/write speeds.
- It avoids disk I/O entirely, which is critical for workloads running alongside GPU inference.
- It is automatically cleaned up on machine reboot, so no stale files survive a Spot VM preemption.

### Payload size guard

Before writing, the child checks the serialized payload size. If it exceeds 15 MB, it replaces the result with a lightweight error payload to avoid saturating the shared memory bus. In practice, only severely malformed documents trigger this guard.

---

## Alternatives considered

| Alternative | Rejected because |
|-------------|------------------|
| `multiprocessing.Manager().dict()` | Pickle serialization overhead; manager process hangs under GPU load |
| `multiprocessing.Queue` | Blocking semantics; implicit serialization; hard to debug full queues |
| Redis or memcached | Adds external dependency; network latency vs. in-memory IPC |
| `mmap` with manual offsets | Complex to manage multiple concurrent writers; no built-in signaling |
| Writing to persistent disk (SSD) | Latency and wear on ephemeral disks; `/dev/shm` is faster and free |

---

## Consequences

### Positive

- **Near-zero IPC latency:** Results are available to the parent microseconds after the child finishes.
- **No serialization overhead:** JSON is written once, directly to memory-backed storage.
- **Resilient to partial writes:** Atomic rename eliminates the risk of reading incomplete data.
- **Self-cleaning:** tmpfs is wiped on reboot; explicit cleanup removes files after use.
- **Predictable memory usage:** Each result exists only briefly in RAM, then is freed.

### Negative

- **Manual polling in the parent:** The parent must loop checking for the sentinel file, which adds complexity compared to a blocking `Queue.get()`.
- **No built-in flow control:** If the parent falls behind, `/dev/shm` can fill up. Mitigated by monitoring free space and enforcing strict payload size limits.
- **Linux-specific:** The solution is tied to Linux tmpfs, but since the entire infrastructure runs on Ubuntu, this is an acceptable constraint.

---

## References

- Linux tmpfs documentation: https://www.kernel.org/doc/html/latest/filesystems/tmpfs.html
- Python `os.rename` atomicity guarantee: POSIX.1-2008
- PaddleOCR multiprocessing pitfalls (GitHub issues)
"""
VRAM Shield — GPU Memory Monitoring & Child Process Recycling

This module implements a defensive GPU memory management layer for the parent
process of a multi-child GPU worker. It monitors VRAM usage without ever
initializing CUDA in the parent (avoiding memory leaks and driver conflicts),
and automatically recycles child processes that exceed safe limits.

Original context: Axima document processing SaaS (GCP, Python 3.10, NVIDIA L4).
Extracted and anonymized for engineering showcase.

Key patterns demonstrated:
- GPU monitoring via pynvml (NVML) without touching CUDA
- Proactive VRAM circuit breaker in the parent health monitor
- Self-eviction when the parent exceeds RAM or lifetime limits
- Child process recycling after N pages processed (memory leak prevention)
- Heartbeat-based hung child detection and forced restart
"""

import os
import gc
import time
import signal
import threading
import psutil
from collections import deque

# ---------------------------------------------------------------------------
# NVML initialization — safe for the parent process
# pynvml talks directly to the NVIDIA driver, bypassing CUDA entirely.
# This is critical: if the parent initializes CUDA, it would conflict with
# child processes that spawn their own CUDA contexts.
# ---------------------------------------------------------------------------
try:
    import pynvml
    pynvml.nvmlInit()
    NVML_AVAILABLE = True
except Exception:
    NVML_AVAILABLE = False


def get_gpu_memory_info():
    """
    Returns (free_gb, total_gb, used_gb) using pynvml.
    Safe to call from the parent process — no CUDA context is created.
    Returns (None, None, None) if NVML is unavailable.
    """
    if not NVML_AVAILABLE:
        return None, None, None
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        free_gb = info.free / (1024 ** 3)
        total_gb = info.total / (1024 ** 3)
        used_gb = info.used / (1024 ** 3)
        return free_gb, total_gb, used_gb
    except Exception:
        return None, None, None


# ---------------------------------------------------------------------------
# Configuration (normally injected via environment)
# ---------------------------------------------------------------------------
NUM_CHILDREN = 3
MAX_PARENT_LIFETIME_HOURS = 24      # Self-eviction after 24h (Spot VM refresh)
MAX_PARENT_RAM_MB = 8192            # Self-eviction if parent RAM exceeds 8 GB
MAX_PAGES_BEFORE_RECYCLE = 300      # Recycle child after 300 pages (VRAM leak prevention)
HEARTBEAT_THRESHOLD = 90            # Seconds without heartbeat → child is hung

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
BOOT_TIME = time.time()
SHUTTING_DOWN = threading.Event()
DRAINING = threading.Event()

# Per-child state (populated at startup)
child_processes = []        # multiprocessing.Process handles
child_heartbeats = []       # multiprocessing.Value('i') — last heartbeat timestamp
child_pages_processed = []  # multiprocessing.Value('i') — cumulative page count

# ---------------------------------------------------------------------------
# Child health monitor (runs in a daemon thread in the parent)
# ---------------------------------------------------------------------------
def child_health_monitor():
    """
    Monitors child health, GPU VRAM, parent RAM, and parent uptime.
    Takes corrective action: drains, kills hung children, restarts them.
    Runs every 10 seconds.
    """
    while not SHUTTING_DOWN.is_set():
        time.sleep(10)
        if SHUTTING_DOWN.is_set():
            break

        # --- Self-eviction: parent lifetime exceeded ---
        uptime_hours = (time.time() - BOOT_TIME) / 3600
        if uptime_hours > MAX_PARENT_LIFETIME_HOURS:
            DRAINING.set()
            break

        # --- Self-eviction: parent RAM exceeded ---
        try:
            parent = psutil.Process(os.getpid())
            ram_mb = parent.memory_info().rss / (1024 * 1024)
            if ram_mb > MAX_PARENT_RAM_MB:
                DRAINING.set()
                break
        except Exception:
            pass

        # --- Proactive VRAM circuit breaker (via pynvml, no CUDA init) ---
        free_gb, _, _ = get_gpu_memory_info()
        if free_gb is not None:
            if free_gb < 1.5:
                # GPU is critically low — stop accepting new work
                if not DRAINING.is_set():
                    DRAINING.set()
            elif free_gb > 2.5:
                # GPU has recovered — resume accepting work
                if DRAINING.is_set() and not SHUTTING_DOWN.is_set():
                    DRAINING.clear()

        # --- Per-child health checks ---
        for i in range(NUM_CHILDREN):
            proc = child_processes[i]
            if proc is None:
                continue

            # Child died unexpectedly
            if not proc.is_alive():
                restart_child(i)
                continue

            # Child heartbeat stale → hung (likely GPU hang)
            with child_heartbeats[i].get_lock():
                last_hb = child_heartbeats[i].value
            if last_hb and time.time() - last_hb > HEARTBEAT_THRESHOLD:
                kill_child(i)
                restart_child(i)


# ---------------------------------------------------------------------------
# Child lifecycle: kill and restart
# ---------------------------------------------------------------------------
def kill_child(child_id: int):
    """Force-kill a child process with SIGKILL to free VRAM immediately."""
    proc = child_processes[child_id]
    try:
        os.kill(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # Orphaned tasks for this child are nacked in the full implementation


def restart_child(child_id: int):
    """
    Replaces a dead or hung child with a fresh one.
    The new child loads models from scratch, recovering any leaked VRAM.
    """
    kill_child(child_id)

    # Create new multiprocessing primitives for the replacement child
    # In the real worker: new Queue, Value, Event, Process are created
    # and inserted at the same list index.
    # start_child_at_index(child_id)  # ← reuses the same slot


# ---------------------------------------------------------------------------
# VRAM cleanup utilities (used by children and parent)
# ---------------------------------------------------------------------------
def force_vram_clear():
    """
    Aggressively free GPU memory. Called by children after each document,
    and by the parent during shutdown.
    """
    try:
        # In child processes with CUDA initialized:
        # import paddle
        # paddle.device.cuda.empty_cache()
        gc.collect()
    except Exception:
        pass
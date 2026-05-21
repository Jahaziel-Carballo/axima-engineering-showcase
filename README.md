# Axima Engineering Showcase

**Axima** is a multi‑tenant document intelligence SaaS I designed, built, and operate on **Google Cloud Platform**.  
It ingests, processes, and extracts structured data from thousands of complex fiscal documents per month using GPU‑accelerated AI and an asynchronous, fault‑tolerant pipeline.

> **Note:** This repository contains **curated engineering samples**, architecture diagrams, and decision records. The full source code remains private to protect proprietary business logic.

---

## Executive Summary

I built Axima from an empty repository to a self‑healing, cost‑aware, multi‑tenant SaaS in production. The system processes complex documents at scale, automatically balances GPU workloads across tenants with fair queuing, and survives Spot VM preemptions without data loss.

This showcase demonstrates deep, hands‑on expertise across four critical areas:

- **Cloud Architecture:** Serverless orchestration (Cloud Run), event‑driven messaging (Pub/Sub), NoSQL design (Firestore), and cost‑optimized GPU compute (Spot VMs with autoscaling).
- **DevOps & Infrastructure:** Full IaC with Terraform and Packer, immutable Docker images, CI/CD pipelines that build, test, and deploy automatically, and observability with custom metrics.
- **MLOps:** GPU worker orchestration, model baking into images, inference validation contracts, and self‑healing child processes that recycle before memory leaks degrade performance.
- **High‑Performance Python:** Multiprocessing, shared‑memory IPC, backpressure‑aware dispatchers, and graceful shutdown sequences that guarantee data safety under hard time constraints.

If you need an engineer who can own the full lifecycle of a cloud product—from code to infra to observability—this repository is my evidence.

---

## Areas of Deep Technical Expertise

### Cloud Architecture (GCP)
Designed a fully event‑driven, multi‑tenant platform on Google Cloud. The system uses **Cloud Run** for serverless HTTP APIs, **Pub/Sub** with ordered delivery and dead‑letter queues for reliable messaging, and **Firestore** with transactional locking to guarantee exactly‑once processing. GPU workers run on **Compute Engine Spot VMs** behind a regional MIG, with autoscaling driven by custom queue‑latency metrics.

### DevOps & Infrastructure as Code
Every piece of infrastructure is defined in **Terraform** and built from **Packer** golden images. The CI/CD pipeline (Cloud Build) constructs GPU‑optimized Docker images, runs Terraform plan/apply, and tags releases with the commit SHA for full traceability. Monitoring, log exclusion filters, and alerting are coded alongside the application, not bolted on later.

### MLOps & GPU Orchestration
The worker spawns multiple child processes that run inference on **NVIDIA L4 GPUs**. VRAM is monitored via NVML without initializing CUDA in the parent process, preventing driver conflicts. A circuit breaker halts ingestion when free GPU memory drops below a safe threshold. Models are baked into the base Docker image during CI, eliminating cold‑start delays.

### High‑Performance Python & Distributed Systems
The codebase solves real distributed‑systems problems in Python:
- **Fairness:** A two‑level round‑robin dispatcher prevents tenant and user starvation.
- **Backpressure:** Bounded queues and semaphores protect GPU workers and cloud APIs from overload.
- **Resilience:** Heartbeat monitors detect hung child processes and force‑restart them. A 25‑second hard‑deadline graceful shutdown safely drains all buffers during Spot VM preemption.
- **IPC:** Results are passed between processes via `/dev/shm` (tmpfs) with atomic writes, avoiding serialization bottlenecks.

### Observability & FinOps
The platform instruments itself. Custom Cloud Monitoring metrics track queue latency,
VRAM pressure, document throughput, and error rates. A dedicated batching thread
ships these metrics every 60 seconds without blocking the inference pipeline.
Project‑level log exclusion filters suppress health‑check noise and debug verbosity
at ingestion time, cutting logging costs before they accumulate. Autoscaling
decisions are driven by real user‑facing signals, not just CPU. GPU compute
schedules align with business hours, and Saturday shutdowns eliminate weekend waste.

## System Architecture

### Component Roles

| Component | Technology | Responsibility |
|-----------|------------|----------------|
| API Orchestrator | Cloud Run + FastAPI | Ingest documents, serve results, enforce multi‑tenant routing. |
| Image Sanitizer | Cloud Run + OpenCV | Validate and normalize uploaded files (PDF, JPG, PNG, TIFF). |
| GPU Workers | Compute Engine (L4 GPUs, Spot VMs) | Run PaddleOCR inference across child processes with VRAM‑aware batching. |
| Messaging | Pub/Sub (ordered delivery, DLQ) | Decouple ingestion from processing; guarantee ordered, exactly‑once delivery per batch. |
| Database | Firestore (native) | Store document metadata, processing state, and heartbeat timestamps. |
| Observability | Cloud Monitoring (custom metrics) | Track queue latency, VRAM usage, throughput, and error rates. |

### Why This Matters for Your Team

- **Full‑cycle ownership:** I took a SaaS idea from zero to a self‑healing, cost‑optimized production system.
- **Failure is engineered away:** Timeouts, circuit breakers, heartbeat monitors, and graceful shutdown protect every document.
- **Infrastructure as code, not as an afterthought:** Everything is automated, versioned, and documented.
- **Deep Python that transfers to Go:** My concurrency patterns map directly to goroutines and channels; I think in distributed systems, not in a single language.
- **Architecture documented as standard practice:** ADRs include trade‑off analysis and rejected alternatives, not just descriptions.

### Tech Stack

| Layer | Technologies |
|-------|---------------|
| Backend & AI | Python, FastAPI, PaddleOCR/PaddleX, OpenCV, NumPy, PyNvML |
| Messaging | Google Cloud Pub/Sub (ordered delivery, dead‑letter queues) |
| Database | Firestore (native mode, collection‑group indexes) |
| Storage | Google Cloud Storage (versioned buckets, CORS) |
| Infrastructure as Code | Terraform (Google provider), Packer (golden images) |
| CI/CD | Cloud Build (Kaniko, multi‑step DAG, Terraform plan/apply) |
| Containers | Docker (multi‑stage, GPU base image with baked models) |
| Observability | Cloud Monitoring (custom metrics), Ops Agent (GPU telemetry) |

### Repository Contents

#### Code Samples

Selected Python snippets that illustrate coding standards and architectural patterns:

| File | Description |
|------|-------------|
| `worker/fairness_dispatcher.py` | Hierarchical round‑robin dispatcher with dual‑level fairness (tenant → user). |
| `worker/graceful_shutdown.py` | Spot VM preemption handler: drains queues, flushes metrics, hard‑exit timer. |
| `worker/vram_shield.py` | GPU memory monitoring without CUDA init, circuit breaker, child process recycling. |
| `worker/paddle_output_validator.py` | Validates and normalizes ML output before it reaches the frontend. |
| `sanitizer/file_type_detector.py` | Robust file type detection via magic bytes and extension, with TIFF support. |
| `worker/async_upload_executor.py` |  Async result persistence with bounded concurrency (ThreadPoolExecutor + Semaphore). |
#### Infrastructure as Code Highlights

| File | Description |
|------|-------------|
| `terraform/main_highlights.tf` | IAM conditions, restrictive egress, autoscaler with custom metrics, shielded VMs. |
| `docker/dockerfile_base_highlights.dockerfile` | Multi‑layer GPU base image with non‑root user, baked models, and layer optimization. |
| `cloudbuild.yaml` | CI/CD pipeline: parallel builds, Terraform plan/apply, immutable image tags. |

#### Architecture Decision Records (ADRs)

- ADR‑001 – IPC via shared memory (tmpfs) instead of `multiprocessing.Manager`
- ADR‑002 – Spot VM strategy with on‑demand fallback
- ADR‑003 – Multi‑tenant fairness in the dispatcher
- ADR‑004 – Asynchronous upload pipeline with bounded concurrency
- ADR‑005 – Guaranteed ordering and exactly‑once processing
- ADR‑006 – Concurrency model: Python today, Go tomorrow
- ADR‑007 – Observability strategy and FinOps‑driven log exclusion

### Contact

**Jahaziel Carballo García**  
📍 Monterrey, NL, Mexico  
📧 jahazielg418@gmail.com  
🔗 [https://www.linkedin.com/in/jahaziel-carballo-640975352/]

> This showcase demonstrates engineering depth without exposing proprietary logic. For a live walkthrough of the full system, I am happy to present it.
> For a detailed document processing flow, see architecture/document-flow.png.
---

### Architecture Diagram

```mermaid
graph TD
    A[Client React App] -->|HTTPS| B[Cloud Run: API Orchestrator]
    B -->|Publish raw doc| C[Pub/Sub: Raw Topic]
    C -->|Push subscription| D[Cloud Run: Image Sanitizer]
    D -->|Publish cleaned doc| E[Pub/Sub: Main Topic]
    E -->|Pull ordered delivery| F[Compute Engine: GPU Worker]

    subgraph F[GPU Worker - g2-standard-12 / L4]
        direction TB
        F1[Parent Process] -->|Spawn & monitor| F2[Child 1: PaddleOCR]
        F1 -->|Spawn & monitor| F3[Child 2: PaddleOCR]
        F1 -->|Spawn & monitor| F4[Child 3: PaddleOCR]
        F1 -->|IPC via /dev/shm| F5[Result Files]
        F1 -->|Dispatch tasks| F6[Fairness Dispatcher Thread]
        F6 -->|Round-robin| F2
        F6 -->|Round-robin| F3
        F6 -->|Round-robin| F4
        F1 -->|Async upload| F7[Upload Executor]
    end

    F -->|Read input| G[Cloud Storage: Input Bucket]
    F7 -->|Write results| H[Cloud Storage: Results Bucket]
    F1 -->|Metadata & heartbeats| I[Firestore]
    B -->|Read metadata| I
    A -->|Fetch results| H
    B -->|Secrets| J[Secret Manager]
    F1 -->|Custom metrics| K[Cloud Monitoring]
    D -->|Custom metrics| K

    E -->|Max retries exceeded| L[Pub/Sub: Dead Letter Queue]
    L -->|Alert/Manual review| M[Ops Dashboard]

    style F fill:#4a90d9,color:#fff
    style B fill:#34a853,color:#fff
    style D fill:#fbbc04,color:#000
    style C fill:#e0e0e0
    style E fill:#e0e0e0
    style L fill:#ff6b6b,color:#fff
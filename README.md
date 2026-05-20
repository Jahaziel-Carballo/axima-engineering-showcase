# Axima Engineering Showcase

**Axima** is a multi-tenant document intelligence SaaS platform I designed, built, and operate on **Google Cloud Platform**.  
It ingests thousands of complex fiscal documents per month, processes them with GPU-accelerated AI, and extracts structured data through an asynchronous, fault-tolerant pipeline.

>  **Note:** This repository contains **curated engineering samples**, architecture diagrams, and decision records. The full source code remains private to protect proprietary business logic and infrastructure configurations.

---

##  What this repository demonstrates

- **System architecture & distributed systems thinking** – multi-tenant fairness, backpressure, graceful degradation
- **Production-grade Python** – multiprocessing, GPU orchestration, async I/O, strict error handling
- **Google Cloud expertise** – Pub/Sub, Firestore, Cloud Run, Compute Engine (Spot VMs), Cloud Build, Terraform
- **Infrastructure as Code & FinOps** – immutable golden images, least-privilege IAM, cost-aware autoscaling
- **Professional engineering practices** – Architecture Decision Records (ADR), health checks, self-healing systems

---

## 🧩 System Architecture

```mermaid
graph TD
    A[Client Browser/React App] -->|HTTPS| B[Cloud Run: API Orchestrator]
    B -->|Publish raw doc| C[Pub/Sub: Raw Topic]
    C -->|Push| D[Cloud Run: Image Sanitizer]
    D -->|Publish cleaned doc| E[Pub/Sub: Main Topic]
    E -->|Pull| F[Compute Engine MIG: GPU Workers]
    F -->|Read/Write| G[Cloud Storage: Input & Results]
    F -->|Metadata| H[Firestore]
    B -->|Read metadata| H
    A -->|Fetch results| G
    B -->|Credentials| I[Secret Manager]

    style F fill:#4a90d9,color:#fff
    style B fill:#34a853,color:#fff
    style D fill:#fbbc04,color:#000
    style C fill:#e0e0e0
    style E fill:#e0e0e0
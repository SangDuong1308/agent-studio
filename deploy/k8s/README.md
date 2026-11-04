# Kubernetes manifests (GKE)

These manifests deploy the full stack to a GKE cluster. They use plain
Deployments/Services so they also run on any cluster (kind, minikube, k3s).

Order of apply:

```bash
kubectl apply -f namespace.yaml
kubectl apply -f secrets.example.yaml   # edit first, or create your own Secret
kubectl apply -f postgres.yaml
kubectl apply -f rabbitmq.yaml
kubectl apply -f gateway.yaml
kubectl apply -f orchestrator.yaml
kubectl apply -f ingestion-worker.yaml
kubectl apply -f agent-runtime.yaml
kubectl apply -f frontend.yaml
```

Images are referenced as `REGISTRY/agent-studio-<service>:TAG`. The CI pipeline
pushes to Artifact Registry and patches the tag. For local testing with kind,
load images with `kind load docker-image ...` and set imagePullPolicy to
`IfNotPresent` (already set).

For production Postgres, prefer Cloud SQL over the in-cluster StatefulSet here
(swap `postgres.yaml` for a Cloud SQL instance + the Cloud SQL Auth Proxy, and
point `DATABASE_URL` at it). The in-cluster Postgres is fine for a demo.

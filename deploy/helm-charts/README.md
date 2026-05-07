# Rucio Storage Testbed — Helm Charts

Kubernetes translation of the `rucio-storage-testbed` docker-compose stack,
following the idioms of [rucio/helm-charts](https://github.com/rucio/helm-charts)
and [rucio/k8s-tutorial](https://github.com/rucio/k8s-tutorial).

## Layout

```
helm-charts/
├── rucio-storage-testbed/        # Umbrella (meta) chart — deploy this
│   ├── Chart.yaml                # Declares deps on all subcharts below
│   ├── values.yaml               # Single source of truth (toggle services, OIDC, etc.)
│   ├── files/                    # Symlinks to repo root (fixed for Helm context)
│   │   ├── certs/    → ../../../certs
│   │   ├── configs/  → ../../../shared/config
│   │   ├── patches/  → ../../../shared/patches
│   │   └── scripts/  → ../../../shared/scripts
│   └── templates/
│       ├── certs-secret.yaml         # All host/CA certs as one Secret
│       ├── configs-cm.yaml           # Shared config files as ConfigMap(s)
│       ├── patches-secret.yaml       # Python patches (rucio fts3.py, constants.py, fts middleware/oidc)
│       ├── rucio-cfg-secrets.yaml    # Pass-through Secrets for rucio-server's secretMounts
│       └── scripts-cm.yaml           # Bootstrap & entrypoint scripts
│
├── fts/                          # Custom image (Dockerfile.fts) — GSI + OIDC FTS server
├── xrootd/                       # rucio/test-xrootd (GSI + SciTokens)
├── storm-webdav/                 # ghcr.io/italiangrid/storm-webdav
├── webdav/                       # rucio/test-webdav (Apache + WebDAV)
├── minio/                        # minio/minio + mc init Job
├── keycloak/                     # quay.io/keycloak/keycloak
└── rucio-client-docker-kubectl/  # Custom image (Dockerfile.rucio-client-docker-kubectl)
```

`ruciodb` / `ruciodb-oidc` reuse `bitnami/postgresql`, and the two Rucio
server deployments reuse the upstream `rucio/rucio-server` chart — both are
declared as dependencies of the umbrella chart.

## Repairing Symlinks

```bash
# Navigate to the umbrella chart's files directory
cd rucio-storage-testbed/files

# Recreate corrected links (4 levels up to reach repo root)
rm -f certs configs patches scripts
ln -s ../../../../certs certs
ln -s ../../../../shared/config configs
ln -s ../../../../shared/patches patches
ln -s ../../../../shared/scripts scripts
ln -s ../../../../shared/tests tests
```

## Quickstart

```sh
# 1. Generate certs (once) from repo root
./scripts/generate-certs.sh

# 2. Create the namespace and install
kubectl create namespace rucio-testbed
helm dependency update helm-charts/rucio-storage-testbed
helm install testbed helm-charts/rucio-storage-testbed --namespace rucio-testbed
```

You should end up with something like:

```bash
$  kubectl get pods -n rucio-testbed
NAME                            READY   STATUS      RESTARTS   AGE
fts-86f4b957cb-trsff            0/1     Running     0          64s
fts-oidc-8556f7f4cf-r69w9       0/1     Running     0          64s
ftsdb-0                         1/1     Running     0          63s
ftsdb-oidc-0                    1/1     Running     0          64s
keycloak-55845db8df-8d4k9       1/1     Running     0          64s
minio1-0                        1/1     Running     0          63s
minio2-0                        1/1     Running     0          64s
rucio-6578b864c9-wpb4p          2/2     Running     0          63s
rucio-bootstrap-db-rz8zc        0/1     Completed   0          63s
rucio-client-574f4bcb48-gkl9x   1/1     Running     0          64s
rucio-oidc-9876d5b9c-gjd56      2/2     Running     0          63s
rucio-oidc-bootstrap-db-pc8bp   0/1     Completed   0          8s
ruciodb-0                       1/1     Running     0          63s
ruciodb-oidc-0                  1/1     Running     0          64s
storm1-0                        1/1     Running     0          64s
storm2-0                        1/1     Running     0          64s
webdav1-7d46f9455b-zmqxp        1/1     Running     0          63s
webdav2-cc677bd59-2rsmt         1/1     Running     0          64s
xrd1-656c4b88b4-948xd           1/1     Running     0          64s
xrd2-65b8b9bcb5-vppgs           1/1     Running     0          64s
xrd3-77875fb57c-9qpwn           1/1     Running     0          64s
xrd4-84686559dd-zxrgv           1/1     Running     0          64s
```

Tear down:

```sh
helm uninstall testbed -n rucio-testbed
kubectl -n rucio-testbed delete pvc --all   # PVCs aren't removed by `helm uninstall`
```

## Design notes

* **Configs, patches and scripts** — everything under `shared/config/`,
  `shared/patches/`, and `shared/scripts/` in the repo — are exposed to pods
  through ConfigMaps or Secrets managed by the umbrella
  chart. The umbrella's `files/` directory contains symlinks into the repo's
  shared/ tree, so the chart and the compose stack consume identical sources
  with no duplication.
* **Certificates** live in a single Secret (`testbed-certs`), populated from
  `files/certs/` (a symlink to `./certs/` at the repo root). Each pod mounts
  only the keys it needs via `subPath`. Regenerate the certs with
  `./scripts/generate-certs.sh` and re-run `helm upgrade`.
* **Service discovery** — every subchart's Service is named to match the
  compose `hostname:` value, so existing config files referencing
  `https://keycloak:8443`, `https://fts:8446`, etc. work without modification.
* **Reuse over reinvention** — `rucio-server` and `postgresql` come from
  upstream charts as dependencies; only services without a usable upstream
  chart (FTS, StoRM-WebDAV, XRootD with missing runtime dependencies, etc.) ship as new local
  charts.
* **OIDC subchart alias** — the second `rucio-server` dependency is aliased
  as `rucio-oidc` (hyphen, not camelCase) because the upstream chart
  templates the alias into a container `name:` field, and Kubernetes
  enforces RFC 1123 there. Values for it sit under the `"rucio-oidc":`
  key in `values.yaml` and are accessed in templates with
  `(index .Values "rucio-oidc")`.

# Autoware ML Param Files

Vendored `.param.yaml` and `deploy_metadata.yaml` files from the
[Autoware ML artifact set](https://github.com/autowarefoundation/autoware/blob/2dbc3fabcceb8513f08488d2cbd5ead148d9fc57/ansible/roles/artifacts/tasks/main.yaml).

## Why

Autoware launch files reference these param YAMLs via paths like
`$HOME/autoware_data/lidar_centerpoint/*.param.yaml`.  When roscope
resolves a `<param from="..."/>` pointing to one of these files, it
reads and inlines the YAML content.

The full artifact set (including large ONNX models) is ~10 GB and managed
by Autoware's ansible provisioning.  We vendor only the lightweight YAML
files needed for launch resolution.

## How they are used

In CI (`autoware-integration.yml`):

```bash
cp -r .github/assets/autoware_data "$HOME/autoware_data"
```

## Updating

When Autoware updates its ML artifacts, re-download the param files from
the ansible role and update the pinned SHA in this README:

```bash
BASE_URL="https://awf.ml.dev.web.auto/perception/models"

# Example: lidar_centerpoint
curl -sSfL -o lidar_centerpoint/centerpoint_tiny_ml_package.param.yaml \
  "$BASE_URL/centerpoint/v3/centerpoint_tiny_ml_package.param.yaml"
```

Refer to the ansible role for the current URLs and versions:
<https://github.com/autowarefoundation/autoware/blob/main/ansible/roles/artifacts/tasks/main.yaml>

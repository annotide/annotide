# Moving from Azure Machine Learning data labeling

Microsoft retired Azure Machine Learning data labeling on 30 September 2026
and asks its users to move to a third-party labeling tool. Its notice says
that workloads still running data labeling are deleted and their application
data lost
([Microsoft Learn](https://learn.microsoft.com/en-us/azure/machine-learning/how-to-label-data)).

Annotide reads the images where Azure Machine Learning left them, in your Blob
Storage, so nothing is copied. It imports the COCO files that data labeling
exported, and it signs people in with Microsoft Entra ID.

## What carries over

| Azure ML project type | Export | In Annotide |
| --------------------- | ------ | ----------- |
| Object identification (bounding box) | COCO | **Imported**: boxes, with your class names |
| Instance segmentation (polygon) | COCO | **Imported**: polygons |
| Image classification (multi-class, multi-label) | CSV, MLTable | Not imported yet: re-label, or pre-label with your own model |
| Semantic segmentation (preview) | MLTable | Not imported yet |
| Text classification, named entity recognition | CSV, MLTable, CoNLL | Not imported yet |

The images themselves never move: Annotide's Azure Blob connector points at
the same storage account and container the Azure ML datastore used.

## What you need

- The **COCO export** of each project. Azure ML wrote it to the workspace's
  default Blob store, in a folder under `Labeling/export/coco`. Projects that
  were never exported before the retirement may be gone.
- The **storage account** holding the images. That is often the workspace's
  own (`workspaceblobstore`).
- An Annotide installation: the Azure Marketplace offer, or the
  [Helm chart](../infra/helm/annotide/README.md).

## Steps

1. **Connect the storage.** On **Connectors**, register an Azure Blob
   connector for the account and container that hold the images. Use the
   managed identity, not a key, and run **Check**: browsers load images straight
   from the storage, so it needs CORS for the app's origin. A storage account
   that only has a private endpoint (common for Azure ML workspaces) works
   only for people on that network ([INSTALL.md §5](INSTALL.md#5-connect-your-storage)).
2. **Create the project.** Set its folder (prefix) to where the images are,
   the same path the Azure ML dataset used.
3. **Recreate the classes** in the label schema with the bounding box or
   polygon tool. Use the Azure ML label names, or map them during the import.
4. **Scan** the source (project page → **Add data**). Scanning reads every
   image's width and height. The import needs them, because Azure ML's COCO
   export stores coordinates as fractions of the image and writes `0` for
   every width and height.
5. **Import.** In **Imports**, choose **COCO**, pick the exported file (upload
   it, or name its path on a connector) and tick **Dry run** first. The result
   shows how many images matched project items, which ones did not, and the
   class names found, so you can map them before the real import.
6. **Import for real** as `submitted` (ready for review) or `draft`. Each
   image gets a new annotation version, authored by you.

Annotide reads Azure ML's image references (`azureml://…/datastores/<name>/paths/<path>`,
`AmlDatastore://<name>/<path>` or a Blob URL) as the `<path>` inside them. When
the project's folder differs, images still match by their file name as long
as that is unique in the project.

## Replacing the rest of Azure ML data labeling

| Azure ML data labeling | Annotide |
| ---------------------- | -------- |
| ML-assisted labeling (clustering, prelabeling) | **Pre-labelling** with your own models: an endpoint that speaks Annotide's model API, or pre-labels posted through the SDK (from an Azure ML job, for example); the queue can be ordered by uncertainty |
| Consensus labeling (preview) | **Consensus**: several annotators per item, with an agreement score; plus gold items |
| Review labels, accept and reject | **Review queue**: approve, reject with a comment, or correct |
| Export to COCO, CSV or MLTable | **Exports** to COCO, YOLO, spaCy, CoNLL and the native format; **snapshots** with train / val / test splits; MLflow for Azure ML training |
| Workspace roles | Entra ID single sign-on with group-to-role mapping ([ENTRA.md](ENTRA.md)) |
| Vendor labeling companies | Any team you invite, limited to folders if needed |

## Help

Migrating a large project, or a format not imported yet? Write to
hello@annotide.com with the project type and roughly how many items it has.

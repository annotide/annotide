# User guide (OPS-8)

For the people who annotate, review and run projects. Administrators
installing the platform should read [INSTALL.md](INSTALL.md) first.

## Roles

| Role | Can |
| ---- | --- |
| **Annotator** | Take tasks from the queue, annotate, save drafts, submit, comment |
| **Reviewer** | Everything an annotator can, plus approve or reject submitted work, correct it, run bulk actions |
| **Owner** | Everything, plus project settings, label schema, members, imports, exports, snapshots, pre-labelling and webhooks |
| **Viewer** | Read only |

An owner can limit an annotator, reviewer or viewer to some **folders** of
the project (Settings → Members → Folders). Items outside those folders do
not exist for that person.

## Annotating

Open a project and press **Start annotating**. The queue hands you the most
urgent open task and locks it to you; **Release** gives it back. Opening an
item from the grid is only browsing and takes no task.

- **Save draft** keeps your work without sending it anywhere.
- **Submit** (Ctrl/⌘ + Enter) checks the result against the label schema and
  sends it to review. A missing required field or a wrong value is listed
  before anything is sent.
- **N** / **P** move to the next / previous item; **Shift + C** copies the
  shapes of the previous saved item.
- Every save is a new version. Nothing you saved is ever overwritten.

### What you see, by kind of item

| Item | How you annotate |
| ---- | ---------------- |
| **Image** | Pick a class, then a tool: Select (V), Box (B), Rotated box (R), Polygon (G), Polyline (L), Point (P), Smart polygon (S, a model draws the outline from a click or a box), Keypoints (J), Brush (K) and Eraser (E) for masks, Superpixels (X), Measure (M). Zoom with + / −, fit with 0. Very large images load tile by tile. |
| **Video** | Boxes as keyframes on a track; frames in between are interpolated. Step frame by frame and mark where an object leaves the view. |
| **Text** | Select text to mark a span of the active class; link two spans with **Link** (R) to make a relation. |
| **PDF** | Boxes and text spans per page; **Read text (OCR)** for scanned pages. |
| **Audio** | Drag across the waveform to mark a segment; type the speaker and the transcript in the list below. **K** plays and pauses; Delete removes the selected segment. |
| **Time series** | Each channel has its own lane. Drag across the plot to mark an interval, and choose whether it covers every channel or only the ones shown. |
| **LLM evaluation** | Read the conversation and the candidate responses. Rank them (move up / down), or pick the better of two. Rate each response, each turn or the whole conversation on the scale. |

**Context.** When the item has companion views (a caption, a second image,
the source text), they appear under **Context** in the right panel. Answer
questions about them, such as whether the caption matches, in the
**Classification** fields.

**Attributes.** Select a shape to edit its attributes in the right panel.

## Reviewing

**Start reviewing** takes submitted work from the review queue.

- **Approve** accepts the version.
- **Reject** sends it back to the annotator with your comment.
- You can correct the result yourself before approving; the correction is a
  new version in your name.

Bulk actions on the item grid (approve, return to queue, assign, tag) apply to many
items at once. Items they do not fit are listed with a reason.

## Comments and notifications

Comment on an item, or on one shape of it. Mention someone with
`@their@email`. Mentions, replies to your comments and review verdicts
appear under the bell. If your administrator has set up e-mail, they also
come by e-mail; turn that off on your **Security** page.

## Running a project (owners)

1. **Storage.** An administrator registers the connector (Connectors page).
   **Check** confirms the platform can reach it and that the browser will be
   allowed to load from it.
2. **Project.** Create it, pick the source (and, optionally, a different
   result) connector, and set the folder (prefix) and file pattern (glob).
   In Settings you can also make same-name files such as `photo.txt` beside
   `photo.jpg` a context view instead of an item.
3. **Label schema.** Classes with their tools, colours, hotkeys and
   attributes; rating classes need a scale. Each save is a new version, and
   old annotations keep pointing at the version they were made with.
4. **Workflow.** Review on or off, consensus (several annotators per item),
   gold items for accuracy checks, priorities and deadlines.
5. **Items.** **Scan** the source, upload files from the browser, or import
   existing annotations (COCO, YOLO, VOC, CVAT, Label Studio).
6. **Pre-labelling.** Run a registered model over new items. Its suggestions
   arrive as drafts that people correct, and they never replace human work.
7. **Members.** Add people with a role, optionally limited to folders.
8. **Results.** Every saved version is written to the result storage as it
   happens. **Snapshots** freeze a dataset (optionally split into
   train / val / test). **Exports** produce COCO, YOLO, YOLO-pose, spaCy,
   CoNLL, LLM preference pairs, segments (with RTTM) or the native format.
9. **Quality.** The dashboard shows progress and throughput; the Quality
   panel shows agreement between annotators and accuracy on gold items.
10. **Integrations.** Webhooks for your own systems (signed JSON), Slack or
    Microsoft Teams messages, and retraining events for ML pipelines (MLflow,
    Databricks, Azure ML).

## Working from code

- **API keys** (Settings → API keys) for scripts and CI; a **service
  account** for anything that is not a person.
- **Python SDK and CLI**: `pip install annotide`, then for example
  `annotide export <project> --format coco -o dataset.zip`
  ([sdk/README.md](../sdk/README.md)).
- **AI agents**: `annotide mcp` lets an MCP client such as Claude pre-label
  a project with a service account's key ([sdk/README.md](../sdk/README.md)).

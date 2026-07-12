VideoHighlighter (Freeware)

A Python tool to automatically generate highlight clips from videos using scene detection, motion detection, audio peaks, object detection, action recognition, and transcript analysis.


Features

Detects:
- Scenes using OpenCV.
- Motion peaks and scene changes.
- Objects
- Actions
- Audio peaks.

Generates transcript subtitles via OpenAI Whisper.
Cuts and merges top scoring segments into a highlight video.
Fully configurable: frame skip, highlight duration, keywords.
Optional GUI for easy interaction.


## Preview

![VideoHighlighter](assets/Highlighter.png)
![Transcript Subtitles](assets/Transcript_Subtitles.png)

## Timeline Viewer
![Timeline Viewer](assets/TimelineViewer.png)

## Visual Search
![Visual Search](assets/VisualSearch.png)

## Action Recognition
![Action Recognition](assets/power_rangers_actions_annotated.gif)

## Workflow Stages
![Workflow Stages](assets/workflow_stages.png)

## Installation

### Windows (recommended)
Download the latest `.exe` from [Releases](link) — no Python or dependencies required.

### Linux / Building from Source
1. **Python & FFmpeg**
   FFmpeg must be installed and available in your system PATH.

## Usage
Linux: python main.py 
Windows: run Videohighlighter.exe
Mac: I think not working, will fix it one day. DMG file is still generated

## Custom Object Detection: Import a Dataset and Train

VideoHighlighter can train its own object detector from a dataset ZIP exported by
[Roboflow](https://universe.roboflow.com/), CVAT, or any tool that produces the
standard YOLO layout (`data.yaml` + `train/valid/test` folders). No manual
extraction needed — point the trainer at the ZIP:

```bash
python training/train_object.py --dataset MyDataset.zip
```

The importer extracts and validates the archive (it also fixes Roboflow's
broken `../` paths for you), then trains starting from the app's configured
base model (`yolo11m.pt` by default).

### The dataset library (growing one model over time)

By default every import **enriches a persistent dataset library** at
`datasets/library/`: class lists are merged by name, label ids are remapped,
and training runs over *everything imported so far*. Import a 2-class threat
dataset today and a 5-class zombie dataset tomorrow, and you get **one 7-class
model** that detects all of them. A few things worth knowing:

- Enrichment always retrains from the base model — fine-tuning the previous
  custom model on only new data would make it forget the old classes.
  Training time grows as the library grows.
- Once a ZIP is imported, the library is self-contained — the ZIP is safe to
  delete (re-importing the same version is detected and skipped anyway).
- `python training/train_object.py --summary` shows what's in the library
  (datasets, classes, image counts) without training anything.
- `python training/train_object.py --retrain` trains on the library as it
  stands, no ZIP needed — useful after tweaking settings or recovering an
  interrupted run.
- `--standalone` trains just the given dataset without touching the library
  (and only there `--weights path/to/best.pt` fine-tunes an existing model).

### Useful flags

| Flag | Default | What it does |
| --- | --- | --- |
| `--epochs` | 100 | Training epochs (early-stops after 20 stale epochs) |
| `--batch` | 16 | Batch size |
| `--img-size` | 640 | Training resolution |
| `--device` | auto | CUDA if available, else CPU (Intel GPU is inference-only) |
| `--project` | `models/` | Where training runs land |
| `--model` | from config | Base checkpoint override (e.g. `yolo11l.pt`) |
| `--config` | `config/config.yaml` | App config the defaults are read from |
| `--force` | off | Re-import a dataset version the library already has |
| `--keep-temp` | off | Keep the temporary ZIP extraction for inspection |

Every run ends with metrics on the held-out test split (mAP50, mAP50-95,
precision, recall, per-class AP), saved to `metrics.json` next to the model.
Re-running never overwrites a previous run — each gets a versioned folder.

### Using the trained model in VideoHighlighter

The trainer prints the exact settings when it finishes. In the GUI:
**Detector type → "Custom model (trained detector)"**, then **Browse...** to
`models/library/weights/best.pt`. Open the object label selector and **select
the classes you want highlighted** — with no classes selected, object
detection is skipped entirely. Or set it in your config's `advanced:` section:

```yaml
yolo_type: custom
yolo_custom_model_path: models/library/weights/best.pt
```

Training runs from a source checkout only (the packaged .exe doesn't include
the trainer). The stock `yolo11m.pt` and standard detection are never touched —
you can switch between standard and custom models at any time.

## Discord
VideoHighlighter occasionally has feelings about your footage. When it does:
[Join the Discord](https://discord.gg/cUPJqPAMmm) and yell in #support, I'm usually around.


## Notes

OpenAI Whisper is MIT licensed — freely usable.

Google Translate API is optional. If using unofficial libraries (googletrans), no API key is needed, but results may break if Google changes endpoints.

This project does not include any paid API keys. Users must provide their own if using official services.


## License

This repository is released under the GNU Affero General Public License v3.0 (AGPLv3). You are free to use, modify, and distribute the code, provided that any modified versions, including those offered over a network, make their complete source code available under the same license.


## Project Background

This project started as a personal tool to automatically generate subtitles for videos, for my young 7 years old son. Over time, it evolved into a highlights generator for movies, sports, and personal videos.

The primary goal remains practical: speed up video analysis, generate highlights, and create accessible subtitles automatically.

![Stars History](assets/star-history-2026630.png)
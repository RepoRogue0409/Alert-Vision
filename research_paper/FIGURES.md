# Figures in the Alert Vision research paper

This file records every figure used in `Alert_Vision_Research_Paper.md`, its
source of truth, and how it was produced. All figures are Mermaid diagrams or
charts embedded directly in the Markdown paper. No screenshots or fabricated
experimental images are used.

## Figure sources

| Fig. | Caption / subject | Type | Source of truth | Location in paper |
|---:|---|---|---|---|
| 1 | Overall system architecture | Mermaid flowchart | `docs/ARCHITECTURE.md` §1–2; `alert_vision.py` constants and run loop; `start.bat`; `serve_static.py` | Section IV |
| 2 | Multi-camera processing pipeline | Mermaid flowchart | `alert_vision.py::CameraPipeline.process_frame()` lines ~875–1089; `analytics.py::LiveAnalyzer.add()` | Section IV |
| 3 | Crowd analytics workflow and alert transition | Mermaid flowchart | `analytics.py` level functions; `alert_vision.py` hysteresis and alert gate lines ~941–1035 | Section V |
| 4 | Dashboard and communication architecture | Mermaid flowchart | `alert_vision.py::WebSocketBroadcaster` lines ~1629–1754; `script.js` `handlePacket`, `queueAlert`, `showAlarm` | Section VI |
| 5 | Mean CPU by camera count | Mermaid bar chart | `docs/PERFORMANCE.md` Table, rows 1/2/4/6 | Section VIII |
| 6 | Mean RSS by camera count | Mermaid bar chart | `docs/PERFORMANCE.md` Table, rows 1/2/4/6 | Section VIII |

## Data provenance for charts

Figures 5 and 6 are plotted only from the measured values in
`docs/PERFORMANCE.md`:

| Cameras | Mean CPU (%) | Mean RSS (MB) |
|---:|---:|---:|
| 1 | 36.5 | 381.4 |
| 2 | 59.4 | 444.6 |
| 4 | 68.0 | 570.8 |
| 6 | 70.5 | 696.9 |

The paper rounds RSS to whole MB in Table III (381, 445, 571, 697) while the
charts use the same underlying values. No interpolated or estimated data points
are added.

## Rendering notes

* Mermaid blocks are embedded as ` ```mermaid ` fenced code blocks.
* GitHub-flavoured Markdown renderers with Mermaid support display the diagrams.
* If a renderer lacks Mermaid support, the paper remains readable because every
  diagram is described by its caption and surrounding text.
* No external image assets are required, and no image-generation tool was used.

## Design constraints

1. Diagrams reflect implemented components only. LWCC is not drawn because it is not wired into the build.
2. No cross-camera identity module is drawn because it does not exist.
3. The alert diagram distinguishes queue acceptance from browser receipt.
4. The processing diagram does not claim detection accuracy; it shows the implemented computational path.
5. The architecture diagram uses the actual ports 8000, 8765, and 8766.

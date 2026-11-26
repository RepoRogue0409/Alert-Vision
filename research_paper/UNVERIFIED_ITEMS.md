# Unverified items and missing information

This file lists everything that the paper does **not** claim as verified, plus
any repository inconsistency discovered during fact-checking. It is deliberately
conservative: absence of evidence is reported as absence, not as a negative result.

## 1. Computer-vision accuracy

| Item | Status | Required to verify |
|---|---|---|
| Person-detection recall / precision at 640 px and `conf=0.25` | NOT VERIFIED | Labelled video with small/distant people |
| Small-person misses at stride 10 | NOT VERIFIED | Labelled stride-10 video comparison |
| ByteTrack identity-switch rate | NOT VERIFIED | Labelled multi-person trajectories |
| Physical people/m² accuracy | ESTIMATE ONLY | Plan-view or instrumented ground truth |
| Risk weights (0.4/0.2/0.2/0.2) against real incidents | NOT CALIBRATED | Incident-labelled dataset and study |
| ETA / time-to-critical | ESTIMATE ONLY | Longitudinal labelled risk events |
| Turbulence/convergence as predictors of crowd incidents | HEURISTIC | Controlled validation study |

## 2. Experimental gaps

| Item | Status | Notes |
|---|---|---|
| 12 simultaneous MJPEG clients × 6 cameras | NOT MEASURED | Only one MJPEG client was measured; paper states this as future work |
| Real DroidCam hardware at `192.168.1.106:4747` | NOT MEASURED | Benchmark used `tools/fake_mjpeg.py` |
| Wireless jitter / phone encoder behaviour | NOT MEASURED | Synthetic streams do not reproduce network conditions |
| GPU inference | NOT MEASURED | Benchmark explicitly used `device="cpu"` |
| Stride-10 performance | NOT MEASURED | Only stride 1 was benchmarked |
| Long-duration memory leakage | NOT MEASURED | Short benchmark windows only |
| Capacity curve with more repetitions | NOT ESTABLISHED | CPU was non-monotonic between 2 and 4 cameras |

## 3. Frontend and human factors

| Item | Status | Notes |
|---|---|---|
| Audible alarm in a noisy venue | NOT VERIFIED | Requires human QA |
| ACK button visual flow | NOT VERIFIED | Source-contract tests only |
| Full tile re-render reduction in a real browser | NOT VERIFIED | Incremental DOM logic is tested from source; no JS runtime installed |
| Operator response to HIGH/CRITICAL | NOT STUDIED | Human-factors research required |
| CSV formula-injection behaviour in Excel | NOT VERIFIED | Code guard exists; manual spreadsheet test remains |

## 4. Security and deployment

| Item | Status | Notes |
|---|---|---|
| Token authentication | NOT IMPLEMENTED | Loopback + Origin allow-list only |
| TLS | NOT IMPLEMENTED | Plain HTTP/WS on loopback |
| Non-loopback deployment | NOT SUPPORTED | All listeners bind to 127.0.0.1 |
| LWCC active counting | NOT WIRED | `ALERT_VISION_LWCC` is reserved; no code reads it |

## 5. Repository inconsistencies discovered

1. **`docs/SECURITY.md` line 37** states that "LWCC remains optional and lazily imported." This contradicts `README.md`, `docs/VERIFICATION.md`, and the source grep, which show no LWCC code path. The paper follows the code and README and reports LWCC as not wired.
2. **`docs/ARCHITECTURE.md`** was written as a Phase 0 audit snapshot and contains pre-fix verdicts such as "alerts share the telemetry queue". A Phase G correction section was added later. The paper uses the corrected state, verified by tests.
3. **`papers/` folder** contains three earlier short paper drafts created before this task. They were not modified and are not the source of the final research paper.

## 6. Document-generation limitations

| Tool | Availability |
|---|---|
| `pandoc` | Not installed |
| `pdflatex` / `xelatex` | Not installed |
| `wkhtmltopdf` / `weasyprint` | Not installed |
| Python `python-docx`, `weasyprint`, `reportlab` | Not installed in the project venv |

Consequently, no DOCX or PDF was generated. The deliverable is Markdown with
embedded Mermaid figures, suitable for conversion when a document toolchain is
available. The page count reported in the final summary is an estimate, not a
typeset pagination.

## 7. Claims deliberately not made

* The system cannot reliably predict or prevent stampedes.
* Track IDs are not unique real-world people.
* No cross-camera identity tracking is performed.
* Uncalibrated density is not a measured people/m² value.
* ETA is not a scientifically validated prediction.
* The four risk levels are decision-support heuristics, not certified safety states.
* The benchmark does not demonstrate accuracy or generalisability to other hardware.

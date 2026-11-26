# Time-Normalized Heuristic Crowd Risk with Explicit Uncertainty

**Draft research paper — v1.0, 25 November 2026**  
**Author:** [Author name], [Affiliation]  
**Artifact:** `analytics.py`, commit `93b7481`

> This paper describes the risk model as implemented. It is a decision-support
> heuristic, not a calibrated life-safety measurement.

## Abstract

Crowd-risk dashboards commonly combine a people count with a smoothing filter and
publish the result as a "risk score". Two subtle defects undermine such systems: the
smoothing strength depends on frame rate, and the rate-of-change term depends on
inference stride rather than elapsed time. We present the analytics model used in
Alert Vision, where the EMA coefficient and the rate term are normalised to a nominal
15 FPS sampling interval, optical-flow velocity is reported in pixels per second,
divergence is motion-masked and radial (zoom) compensated, and calibrated density can
only increase risk. We also describe an ETA estimate that publishes its slope, R²,
sample count, and an explicit caveat. Ten synthetic scenarios are used as regression
tests. The paper states plainly which properties are verified and which require a
labelled dataset.

**Keywords:** crowd density, optical flow, exponential moving average, risk scoring,
uncertainty communication.

---

## 1. Introduction

The CrowdShield reference model defines four levels (SAFE, MODERATE, HIGH, CRITICAL)
from a weighted sum of density, rate of change, convergence, and turbulence, plus
physical density gates at 4 and 6 people/m². Porting this model to a variable-stride,
multi-camera Python pipeline exposed a mismatch: the mathematics assumed a fixed
sample interval, but the implementation called `add()` once per *processed frame*, not
once per *second*.

This paper formalises the corrected model and the honesty rules adopted around it.

## 2. Model definitions

### 2.1 Signals

For a processed frame at time \(t\) with \(n_t\) detected people:

* count-normalised density:  
  \(d^{count}_t = \mathrm{clip}((n_t - (m-1))/(m+4-(m-1)), 0, 1)\), where \(m\) is
  `min_people`.
* calibrated density (when area \(A>0\) is known):  
  \(p_t = n_t / A\),  
  \(d^{psm}_t = \mathrm{clip}((p_t - 1)/(6 - 1), 0, 1)\).
* combined density term: \(d_t = \max(d^{count}_t, d^{psm}_t)\).  
  Calibration can add risk but never mask it.

### 2.2 Time-normalised rate of change

Let \(\Delta t\) be the measured interval between analysed frames (including stride),
clamped to [0.001, 2] s. The first sample has no rate history and contributes zero.
For later samples:

\[
r_t = \mathrm{clip}\!\left(\frac{\max(d_t - d_{t-1}, 0)}{\Delta t \cdot R}, 0, 1\right),
\qquad R = 2.0\ \text{per second}.
\]

This removes the stride dependence: the same real-world rise over one second scores
the same at 5 FPS and 15 FPS.

### 2.3 Time-constant EMA

With reference alpha \(\alpha_{15} = 0.25\) at \(\Delta t_{15}=1/15\) s, the effective
alpha is

\[
\alpha(\Delta t) = 1 - (1-\alpha_{15})^{\Delta t / \Delta t_{15}}.
\]

Both the turbulence smoother and the risk smoother use this coefficient, so three
seconds of constant input produce the same smoothed value regardless of frame rate.

### 2.4 Optical flow

Farneback flow is computed on half-resolution grayscale frames. Global translation is
removed by subtracting the per-axis median. Divergence is then computed only on moving
pixels (`magnitude > 0.3`). A least-squares radial term
\(\mathbf{f} \approx a(x-c_x, y-c_y)\) is removed to suppress camera zoom. Divergence
is normalised per second by the measured \(\Delta t\), and velocity is reported as
mean flow magnitude divided by \(\Delta t\) (pixels/s).

### 2.5 Level mapping and hysteresis

The risk score is mapped with `level_for`: CRITICAL at \(r \ge 0.85\), HIGH at
\(r \ge threshold\), MODERATE at \(r \ge 0.6\,threshold\), else SAFE. The HIGH
threshold has one source of truth: `alert_threshold` is mirrored into
`risk_thresholds["high"]` during config validation, preventing formula-HIGH and the
alert gate from diverging.

Density-forced levels (4/m² → HIGH, 6/m² → CRITICAL) escalate immediately. A majority
vote over the last eight density levels is required before clearing, so a single noisy
frame cannot flap the state.

Time hysteresis then requires 1.5 s to escalate and 4 s to de-escalate.

### 2.6 Forecast estimate

The ETA is a least-squares linear fit over the most recent EMA samples, computed
manually (no `np.polyfit` RankWarning). It is returned only when at least six samples
span at least two seconds, the slope is positive, and the estimate is within a 180 s
horizon. The published object contains `eta_s`, `slope_per_s`, `r2`, `samples`, and a
caveat string. The UI renders `~Xs (est)`. Absence of ETA is explicitly
"no reliable estimate", never "no risk".

## 3. Synthetic scenario coverage

Ten scenarios were implemented as regression tests:

1. Rate term identical at 5 and 15 FPS for the same one-second rise.
2. EMA value identical after three seconds at 5 and 15 FPS.
3. Flow velocity and divergence scale with measured `dt`.
4. A synthetic zoom flow is largely removed; static pixels produce no divergence.
5. Calibrated density raises risk; 5 people/1 m² → HIGH, 6 → CRITICAL.
6. ETA = 3.0 s for a known linear trend; flat and duplicate-timestamp series return
   no estimate.
7. Density vote requires five SAFE samples to clear four HIGH votes.
8. Cumulative ByteTrack IDs are labelled "NOT unique people".
9. HIGH threshold mirroring survives config updates and clamping.
10. Frame pre-processing contract: width ≤640, stride clamped to 1–10.

All ten pass as part of the 64-test suite.

## 4. Verified and unverified properties

| Property | Status |
|---|---|
| Level For mapping and density gates | Verified by tests |
| FPS/stride independence of rate and EMA | Verified by scenarios 1–2 |
| Zoom compensation and motion masking | Verified by scenario 4 |
| Honest ETA with caveat/R² | Verified by scenario 6 |
| Cumulative track-ID labelling | Verified by scenario 8 |
| Detection recall at 640 px / conf 0.25 | **Not verified** — needs labelled video |
| ByteTrack ID-switch rate | **Not verified** — needs labelled trajectories |
| Physical density accuracy | **Estimate only** — single uncalibrated camera geometry |
| Turbulence/convergence as crowd-safety predictors | **Heuristic signals**, not validated predictors |

## 5. Conclusion

The main contribution is not a new risk formula but the *temporal normalisation* and
*honesty envelope* around an existing heuristic model. By tying smoothing and rate to
measured time, and by publishing uncertainty metadata instead of a bare ETA, the system
avoids implying precision it does not have. Future work should validate the heuristic
weights against labelled crowd events before any safety claim is made.

## References

See `references.bib`.

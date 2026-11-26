# Changelog

## 0.8.0

- Accept plausible fast-room heating samples up to 6 °C/hour, including the
  previously rejected 3.745 °C/hour diagnostic case.
- Replace the discontinuous early-learning weighting with monotonically increasing
  confidence, so adding the third sample cannot return a zone to a conservative
  maximum lead.
- Treat learned rates as effective override-to-target rates and avoid counting the
  configured initial warm-up delay again after learning begins.
- Blend the latest observed controlled heat-up duration into the next prediction as
  bounded timing feedback. Give a rejected legacy sample extra recovery weight and
  limit any one-cycle adjustment to 60 minutes.
- Report switchpoint comfort and start timing separately, with early/on-time/late
  timing classification and a rolling timing success percentage.
- Show model lead, timing feedback, final lead, and clearer manual-flow curve/current
  labels on the example dashboard.

## 0.7.0

- Retrieve supported hourly, daily, and twice-daily forecasts directly from the
  configured `weather.*` entity using Home Assistant's response-returning
  `weather.get_forecasts` action; refresh successful forecasts once per hour and
  retry transient startup/provider failures after five minutes.
- Calculate a stable design outdoor temperature from current conditions and the
  three coldest forecast values within a configurable 48-hour horizon.
- Compare the stable calculated requirement with a configured current manual boiler
  setting and recommend a change only when it differs by at least 3 °C by default.
- Never recommend lowering the manual setting when forecast data is unavailable;
  retain current-weather protection for necessary increases.
- Show keep/increase/decrease advice, forecast coverage, calculated requirement, and
  the update-after-changing reminder on the dynamic dashboard.
- Add forecast retrieval and hysteresis regression tests.

## 0.6.0

- Record a bounded temperature trace during owned preheats and passive follow-up,
  then publish final-half-degree dwell, mid/terminal heating-rate ratio, terminal
  assessment, and 30-minute overshoot for each zone.
- Add one system-level `Suggested flow temperature` diagnostic sensor implementing
  a configurable, bounded linear weather curve. The initial 55 °C at 7 °C outdoor
  curve changes by 1 °C per outdoor degree and is bounded to 50–65 °C.
- Keep the suggestion advisory-only and expose its curve value, evidence adjustment,
  confidence, reason, outdoor temperature, and configuration as attributes.
- Raise the weather-curve recommendation by only 2 °C, and only when at least two
  independent zones have recent, comparable-weather cold outcomes with a terminal
  capacity-limit signature. Never lower it from warm outcomes automatically.
- Extend the dynamic dashboard with the manual flow suggestion and per-zone terminal
  approach diagnostics.

## 0.5.0

- Record actual temperature error at each controlled schedule switchpoint, including
  the observation delay caused by Evohome polling.
- Record the first observed target-reach time as minutes early or late; passively
  observe cold rooms for up to two hours after the switch without issuing any
  further heating command.
- Persist up to 60 outcomes and publish rolling ten-cycle on-time percentage, mean
  absolute temperature error, warm/cold bias, and mean arrival offset.
- Add a dynamic dashboard accuracy table and controller regressions for early, late,
  and timed-out outcomes.

## 0.4.2

- Replace the static one-room dashboard example with a native, dynamically generated
  multi-zone overview requiring no custom cards, helpers, or maintained entity list.
- Show active preheats, schedule transitions, predicted starts, learning rates,
  sample counts, outdoor temperature, last-cycle results, and evaluation times.
- Include an optional native control-card template for rooms where direct dashboard
  controls are desired.

## 0.4.1

- Discover official Evohome entities that have no config-entry ID, as expected for
  Evohome's YAML-loaded legacy entity platform.
- Retain config-entry scoping when available and continue requiring the official
  `evohome` platform plus live heating-zone schedule attributes in the fallback.

## 0.4.0

- Discover every available heating zone belonging to the selected official Evohome
  controller and present the unconfigured zones in one multi-select list.
- Select all discovered zones by default, skip existing zone subentries, and apply
  the system's current prediction defaults to every selected zone.
- Keep all bulk-added zones disabled until individually reviewed and enabled.
- Reload the integration once after the bulk operation so all new zone devices and
  entities appear without restarting Home Assistant.
- Add tests for controller-scoped discovery, entity filtering, duplicate exclusion,
  friendly-name ordering, and inherited defaults.

## 0.3.2

- Keep ownership across a schedule switchpoint while the official Evohome
  integration still exposes its previous poll, reporting
  `waiting_for_schedule_sync` instead of misclassifying the owned override as a
  manual override.
- Reset an override after the switchpoint only when both its target and expiry
  still identify it as the integration's own; continue protecting changed user
  overrides.
- Send the zero duration as the JSON-safe string `00:00:00`, avoiding Recorder
  serialization warnings while preserving Evohome's next-switchpoint semantics.
- Add controller regressions for stale switchpoint data, post-switch reset, manual
  override protection, and JSON-serializable action data.

## 0.3.1

- Classify the controller as a hub so Evohome Optimum Start appears under
  **Settings → Devices & services → Integrations**, rather than Helpers.

## 0.3.0

- Represent one Evohome controller as one integration entry with independently
  configurable zone subentries.
- Share one state listener and one one-minute evaluation timer across all zones.
- Migrate a v0.2.x single-zone entry in place while preserving its learning store,
  entity unique IDs, device, and enabled state.
- Re-evaluate time-dependent decisions every minute and cover the overnight
  `already_at_next_target` regression with pure and Home Assistant runtime tests.
- Publish `last_evaluated` and retain lead, cap, and predicted-rate diagnostics
  throughout an active preheat.
- Keep detailed diagnostic entities and the reset button disabled by default while
  retaining all details on the status entity.

## 0.2.2

- Fix lead-time evaluation crashing when publishing `predicted_rate` twice.
- Prefer the climate entity's public current temperature, retaining the nested
  Evohome temperature only as a fallback.

## 0.2.0

- Accept a temperature sensor, a weather entity, or an assumed outdoor temperature.


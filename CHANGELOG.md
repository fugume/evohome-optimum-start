# Changelog

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


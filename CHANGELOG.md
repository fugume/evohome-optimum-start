# Changelog

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


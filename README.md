# Evohome Optimum Start for Home Assistant

An installable, self-learning optimum-start controller for the official Home
Assistant **Honeywell Total Connect Comfort (Europe)** / Evohome integration.

It does one thing: for each configured room, it asks Evohome for the *next native
scheduled setpoint* and starts that setpoint early enough for the room to be warm at
the switchpoint. One integration entry represents the Evohome system; its zone
subentries keep independent learning and controls while sharing one timer and source
listener. Evohome continues to own its schedules, TRVs, heat demand, boiler
relay/modulation, and fail-safe behavior.

Newly configured zones start **disabled**. No Evohome setting is changed until you
review the diagnostic entities and turn on that zone's `Enabled` switch.

## What is verified against current Home Assistant

This implementation uses the current official Evohome entity contract:

- A zone exposes `status.setpoints.this_sp_temp`, `next_sp_temp`, and
  `next_sp_from`, plus `status.setpoint_status.setpoint_mode`.
- The controller exposes `status.system_mode_status.mode`.
- `evohome.set_zone_override` accepts `setpoint` and optional `duration`.
- A duration of zero is a documented special case: the official integration fetches
  the schedule and sets the override expiry to `next_sp_from`. Omitting `duration`
  would instead create an indefinite override.
- Evohome zones now also have a reset button in Home Assistant. This integration
  does not need its entity ID; when it must cancel an override that it positively
  identifies as its own, it uses the zone's standard `preset_mode: none`, which the
  official integration maps to the Evohome zone reset API.

Primary references:

- [Home Assistant Evohome documentation](https://www.home-assistant.io/integrations/evohome/)
- [Official `set_zone_override` action reference](https://www.home-assistant.io/actions/evohome.set_zone_override/)
- [Current Home Assistant Evohome climate implementation](https://github.com/home-assistant/core/blob/dev/homeassistant/components/evohome/climate.py)
- [Current action schema](https://github.com/home-assistant/core/blob/dev/homeassistant/components/evohome/services.py)

## Safety behavior

The controller will start a preheat only when all of these are true:

1. Its own `Enabled` switch is on.
2. The Evohome system mode is exactly `Auto` or `AutoWithReset`.
3. The zone is in `FollowSchedule` (not a user temporary/permanent override).
4. The next native switchpoint raises the scheduled temperature.
5. The room is below that next target.
6. The calculated start time has arrived and is no more than the configured cap
   (three hours by default).

It records the target, expiry, and start conditions before it sends an override. On
subsequent updates it treats an override as its own only if the live temporary target
still matches. A user changing the target takes ownership immediately: learning stops
and the user override is left alone.

If Away, Heating Off, Day Off, Custom, or another protected system mode begins during
an owned preheat, the integration resets only its own zone override and stands down.
Disabling the integration behaves the same way. At the normal schedule switchpoint,
the zero-duration Evohome override expires by itself. Because the official Evohome
integration normally polls every five minutes, Home Assistant can continue exposing
the pre-switch `TemporaryOverride` briefly after that time. During this interval the
controller retains ownership, reports `waiting_for_schedule_sync`, and does not make
a new optimum-start decision. It completes the cycle when a newer schedule is
visible. If fresh data proves that the exact owned override survived its expiry, the
controller safely resets it; a changed user override is never reset.

## Learning model

Each completed preheat stores:

- starting and ending room temperature;
- target and elapsed minutes;
- outdoor temperature (live or the configured fallback); and
- whether the target was reached before the switchpoint.

The integration keeps the latest 60 accepted cycles in Home Assistant's `.storage`
area. It fits a small, recency-weighted model:

`heating rate = rate at 10 °C outdoors + outdoor-temperature slope`

The initial rate defaults to 0.7 °C/hour. With fewer than three samples, measured
rates are blended conservatively with that value. Implausible/tiny cycles are ignored.
Predicted lead is:

`warm-up delay + temperature rise / predicted rate + safety margin`

and is capped at 180 minutes by default. This is well suited to learning your normal
55 °C flow-temperature setup. A material flow-temperature change changes the plant's
performance; press `Reset learning` after such a change.

An outdoor entity is optional. With none selected, the controller uses the configured
assumed outdoor temperature (7 °C by default). It can still learn a useful base rate,
but cannot learn weather compensation until a live source is added. A temperature
sensor or a `weather.*` entity can be selected directly; for weather entities the
integration reads the current `temperature` attribute.

This model learns from the controller's own historical preheat cycles rather than
querying Recorder. That keeps it independent of Recorder retention/purge settings and
ensures it learns only periods when the integration knows the requested target and
expiry.

### Prediction accuracy

For every preheat it controls, the integration records the temperature observed at
the scheduled switchpoint and calculates:

- **temperature error:** actual minus target temperature, so a negative value means
  the room was cold and a positive value means it was warm;
- **arrival offset:** the first observed target-reach time relative to the
  switchpoint, reported as minutes early or late; and
- **on time:** whether the switchpoint temperature was within the zone's configured
  target tolerance (0.2 °C by default).

If a room is still cold, the integration passively observes it for up to two hours
after the switchpoint to measure late arrival. This observation never changes the
setpoint and stops if the user overrides the zone, the schedule changes, the system
leaves Auto, or the zone controller is disabled.

The status entity retains up to 60 outcomes and publishes rolling values over the
latest ten: on-time percentage, mean absolute temperature error, warm/cold bias, and
mean arrival offset. Evohome and room-temperature polling limit timing precision, so
each outcome also records how late the switchpoint observation itself occurred.
Resetting learning clears both heating samples and accuracy history.

## Installation

Version 0.3.0 and later require Home Assistant 2026.11 or newer because they use Home
Assistant's current config-subentry UI for adding and tuning zones.

### Manual installation (works with this download)

1. Copy the directory
   `custom_components/evohome_optimum_start` into Home Assistant's
   `/config/custom_components/` directory.
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Add integration**.
4. Search for **Evohome Optimum Start**.

The official Evohome integration must already be configured and its entities must be
available. No Evohome username/password is used or stored by this integration.

### HACS after placing this project in a Git repository

Add the repository as a HACS custom repository of type **Integration**, install it,
restart Home Assistant, and follow steps 3–4 above.

## Configure and test one sample zone

Start with a room that has a predictable morning temperature increase, such as the
living room.

### 1. Confirm the source data

In **Developer Tools → Template**, replace the sample entity IDs:

```jinja2
{% set z = state_attr('climate.living_room', 'status') or {} %}
{% set c = state_attr('climate.my_home', 'status') or {} %}
Zone mode: {{ (z.get('setpoint_status') or {}).get('setpoint_mode') }}
Current scheduled target: {{ (z.get('setpoints') or {}).get('this_sp_temp') }}
Next target: {{ (z.get('setpoints') or {}).get('next_sp_temp') }}
Next time: {{ (z.get('setpoints') or {}).get('next_sp_from') }}
System mode: {{ (c.get('system_mode_status') or {}).get('mode') }}
```

Expected values include `FollowSchedule`, numeric temperatures, an ISO timestamp, and
`Auto` (or `AutoWithReset`). If these are absent, update/reload the official Evohome
integration before proceeding.

### 2. Add the Evohome system

Select:

- **Evohome system controller:** the whole-house Evohome climate entity.
- **Outdoor temperature sensor or weather entity:** optionally select a reliable
  temperature sensor or a `weather.*` entity. Leave it blank if you do not have one.

Keep the initial defaults:

| Setting | Initial value | Meaning |
|---|---:|---|
| Initial heating rate | 0.7 °C/h | Conservative starting estimate |
| Assumed outdoor temperature | 7 °C | Used only when no live entity is selected |
| Maximum lead | 180 min | Three-hour hard cap |
| Warm-up delay | 10 min | Valve/radiator response time |
| Safety margin | 10 min | Early-model uncertainty allowance |
| Target tolerance | 0.2 °C | Counts target as reached |
| Minimum learning rise | 0.3 °C | Rejects noisy/tiny samples |

After the system is created, Home Assistant immediately opens **Discover zones**.
The integration finds available heating zones from the selected controller's
official Evohome platform, excludes the controller and existing zones, and selects
all results by default. Evohome is normally loaded from YAML and its entities have no
config-entry ID, so discovery also verifies the live heating-zone schedule attributes
before offering them. For an initial one-room test, deselect everything except the
living room. Every added zone inherits the displayed system defaults and starts
disabled.

For an existing v0.2.x installation, restart Home Assistant after copying v0.3.0.
The old single-zone entry is migrated automatically into one system entry with that
zone beneath it. Its enabled state, learning samples, entity IDs, and device are
preserved. Do not delete and recreate the entry before testing the migration.

### 3. Inspect while disabled

Open the new zone device and inspect the default entities:

- `Status`
- `Predicted start`
- `Preheating`
- `Enabled`

The detailed `Lead time`, heating-rate, sample-count, and `Reset learning` entities
are created disabled to avoid clutter. Enable any you want from the zone's entity
list. The `Status` entity always exposes the same details as attributes, including
`last_evaluated`, the next switchpoint, scheduled targets, system/zone modes,
outdoor temperature and source, lead cap, learned slope, and last cycle result.

Useful states are:

- `disabled` — safe initial state;
- `waiting_for_predicted_start` — normal and ready;
- `waiting_for_capped_start` — estimated need exceeds the configured cap;
- `protected_manual_zone_override` — a user override is active;
- `protected_system_mode_*` — not in Auto/AutoWithReset;
- `preheating` — this integration owns a temporary override.
- `waiting_for_schedule_sync` — the switchpoint has passed and the controller is
  waiting for a fresh Evohome poll before releasing ownership.

### 4. Optional direct action check

Home Assistant's official action can be checked independently in **Developer Tools →
Actions**. Use a safe setpoint and this exact zero duration:

```yaml
action: evohome.set_zone_override
target:
  entity_id: climate.living_room
data:
  setpoint: 20
  duration: "00:00:00"
```

The zone should report `TemporaryOverride`, expiring at the next schedule switchpoint.
Press the official Evohome zone reset button afterwards if you do not want to wait.
Do not omit `duration`: omission means indefinite/permanent.

### 5. Enable one zone

Turn on the Optimum Start device's `Enabled` switch. Watch the first two or three
preheat cycles. Confirm:

- the temporary override target equals the next scheduled target;
- its expiry is the native next switchpoint;
- the zone returns to `FollowSchedule` at that switchpoint; and
- `Learning samples` increases after a useful temperature rise.

The official Evohome cloud integration polls every five minutes by default, so status
changes can cross a native schedule switchpoint before Home Assistant receives the
new state. Prediction is checked every minute, but practical start-time resolution is
bounded by the Evohome poll/update interval. The controller explicitly preserves
ownership through this boundary rather than interpreting the stale state as a manual
override.

The controller deliberately uses the climate entity's public `current_temperature`
attribute. Current Home Assistant may populate this from Evohome's separately polled
high-precision temperature API; the nested `status.temperature_status` value can be
older and is used only as a fallback.

### Add weather later

You can begin with the 7 °C fallback. After adding any Home Assistant weather
integration, open **Settings → Devices & services → Evohome Optimum Start → Configure**
on the system entry and select its `weather.*` entity as the outdoor source. It is
shared by all zones. Existing zone learning is kept, and each zone begins learning
the outdoor-temperature effect as new cycles are collected.

## Scale to all zones

Open the existing Evohome Optimum Start system entry and choose **Discover zones**.
All available zones that are not configured yet are selected by default; review the
list and submit it once. Do not add another integration entry for the same Evohome
controller. Unavailable or disabled official Evohome entities are not offered until
they have a live zone state.

Every zone still has its own enable switch, three-hour cap, learning history,
predicted start, and diagnostics. Bulk-added zones remain disabled, so enable and
observe a few rooms at a time to see how simultaneous demand affects learned rates.

Use **Tune zone** on a zone subentry to change that room's cap or model settings.
**Configure** on the parent system changes the shared weather source and the defaults
offered to zones added later.

## Dashboard

[`examples/dashboard.yaml`](examples/dashboard.yaml) is a complete native Home
Assistant dashboard. It needs no custom cards, helpers, or zone entity-ID list. Its
Markdown cards identify the integration's status sensors by their diagnostic
attributes, so zones added later appear automatically.

To install it as a separate dashboard:

1. Create a blank dashboard in **Settings → Dashboards**.
2. Open it, choose **Edit dashboard → Raw configuration editor**.
3. Replace the raw configuration with the contents of
   `examples/dashboard.yaml`, then save.

The overview shows configured/enabled counts and active preheats. The other tables
show each zone's current and scheduled temperatures, next switch, predicted start,
prediction accuracy, lead, learned rate, sample count, outdoor temperature, last
cycle, and latest evaluation. Enable switches remain on the individual zone devices
by design. A commented native entity-card example at the end of the file can be
copied into the view if direct controls are wanted for a particular room.

Do not copy Evohome schedules into Home Assistant. Do not create direct TRV or boiler
automations for this feature. Leave Evohome Optimum Start disabled on the physical
controller to prevent two optimizers from competing, but leave all ordinary Evohome
schedules enabled.

## Tuning guidance for the described Worcester/Evohome system

- Keep the model at the normal ~55 °C flow temperature while it learns.
- Leave Advanced Load Scaling as-is initially. If you later enable it or substantially
  change flow temperature/weather compensation, reset learning in every zone.
- Do not reduce the three-hour cap until diagnostics show ordinary calculated leads
  stay well below it on cold days.
- If rooms consistently arrive late after 10–15 accepted samples, increase safety
  margin in five-minute increments. If consistently early, reduce it gradually.
- A room that frequently heats while other zones call may learn a different apparent
  rate from an isolated room. That is expected: the model learns observed whole-system
  behavior, including boiler modulation and concurrent demand.

## Removing or pausing

- To pause one room, turn off its `Enabled` switch. If it owns a live preheat, only
  that matching override is reset.
- Removing one zone subentry removes only that room. Disable it first if it is
  currently preheating.
- Removing the parent integration entry removes every configured zone. Home
  Assistant may retain its `.storage`
  file; this contains only numeric learning samples and entity IDs, no credentials.

## Local validation

From this project directory:

```bash
python3 tools/validate.py
python3 -m unittest discover -s tests -v
```

The validation checks Python syntax, JSON files, required integration files, and
translation parity. Unit tests exercise sample rejection, regression, outdoor
compensation, the maximum-lead cap, overnight re-evaluation, switchpoint polling lag,
manual-override protection, and JSON-safe service payloads. Controller/service tests
run when the Home Assistant Python package is installed and otherwise report as
skipped; the pure model and decision tests always run.


# Temperature fixture provenance

`systeminfokit_temperatures.json` contains numeric/state excerpts from the public
[Kyome22/SystemInfoKit measured values](https://github.com/Kyome22/SystemInfoKit/tree/326b3517925567292ab30908d067638f932b0587/MeasuredValues),
pinned to commit `326b3517925567292ab30908d067638f932b0587` (checked 2026-10-01).
Upstream is Copyright 2020 Takuto Nakamura, Apache-2.0. These are selected factual
measurements, not a copy of the Swift implementation or complete device dumps.

Each `capture` identifies the exact upstream JSON filename without `.json`.
The `AppleSmartBattery` and optional `AppleSmartBatteryPack` objects come from the
corresponding subdirectories. Only fields needed for temperature, power, state,
and remaining-energy regression checks are retained. Identifiers, serials,
manufacturing data, and lifetime raw data are omitted. Tests render these excerpts
as ioreg text for the primary service and XML plist for the pack. They do not
claim to be newly captured macOS output.

| Capture | Temperature | VirtualTemperature | Expected °C |
| --- | ---: | ---: | ---: |
| MacBook_Pro_M4_Max_macOS_26_onBattery | 3049 | 3169 | 30.49 |
| MacBook_Pro_M1_Pro_macOS_15_appleAdapter | 3115 | 3829 | 31.15 |
| MacBook_Air_M4_macOS_26_appleAdapter | 2999 | 2679 | 29.99 |
| MacBook_Pro_M5_macOS_26_appleAdapter | 3103 | 3709 | 31.03 |
| MacBook_Air_M2_macOS_27_appleAdapter (pack BatteryData) | 3359 | 3359 | 33.59 |
| MacBook_Air_M3_macOS_27_unknownAdapter (pack BatteryData) | 2989 | 2989 | 29.89 |
| Mac_mini_M4_macOS_26_noBattery | absent | absent | unavailable |

The pinned [BatteryRepository.swift](https://github.com/Kyome22/SystemInfoKit/blob/326b3517925567292ab30908d067638f932b0587/Sources/SystemInfoKit/Repositories/BatteryRepository.swift)
uses top-level `AppleSmartBattery.Temperature` before macOS 27 and
`AppleSmartBatteryPack.BatteryData.Temperature` on macOS 27+, dividing either by
100.0. The [upstream measured-value notes](https://github.com/Kyome22/SystemInfoKit/blob/326b3517925567292ab30908d067638f932b0587/MeasuredValues/README.md)
explain the move and the limitations of the normalized captures. A raw value
around 3000 is compatible with both conversion guesses; plausibility alone
cannot establish a deci-Kelvin unit. The parser follows the upstream conversion.

The captures demonstrate that `VirtualTemperature` is not an interchangeable
alias: it can be higher or lower than `Temperature`. The reviewed public sources
do not establish its exact macOS sensor or calculation. An
[iLEAPP reverse-engineering discussion](https://github.com/abrignoni/iLEAPP/issues/1870)
describes an iOS/iPadOS BDC field with that name as modeled cell temperature, but
explicitly requires further verification and does not establish macOS IOKit
semantics. We therefore do not label it as a CPU, surface, or physical cell
sensor, or silently substitute it for battery temperature.

Additional test cases (invalid values, conflicting nested keys, multiple packs,
errors/timeouts, and `PRIVATE_SENTINEL` strings) are synthetic. The tests check
query counts, exact source selection, diagnostic redaction, uninterrupted power
sampling, and unchanged adaptive delay decisions. Native `ioreg` execution on
macOS 26/27 is not covered by these offline tests.

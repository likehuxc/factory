# D7 source-function acceptance matrix

This matrix is the release gate for preserving existing D7 behavior. A row is complete only when its service, page and automated or hardware test all exist.

| Source | D7 capability | New page/service | Verification |
|---|---|---|---|
| `ota` | PMU IAP upgrade, firmware validation, CRC32, checksum, ACK, retry, cancel | Upgrade / firmware | Golden frames + simulated workflow + PMU bench |
| `ota` | Battery upgrade, wake-up, close-CAN command, delayed start, compatible ACK | Upgrade / firmware | Golden frames + battery bench |
| `ota` | Battery query/control, light control, MachineInfo | Whole machine | Protocol fixtures + robot acceptance |
| `ota` | Raw Classic CAN / CAN FD console | Whole machine | Frame validation + USBCANFD-200U |
| `jihua_motor_quick_config/python` | AA55 scan 0–255 and connection retry | Motor / 485 | Parser/noise fixtures + converter bench |
| `jihua_motor_quick_config/python` | Read/change ID, EEPROM, reset and read-back | Motor / 485 | Simulated serial + motor bench |
| `jihua_motor_quick_config/python` | Enable, brake, velocity and relative position | Motor / 485 | Stop-on-cancel test + motor bench |
| `jihua_motor_quick_config/python` | Forward/reverse loop test | Motor / long test | Cancellation/finally test + motor bench |
| `canfd-link-diag` | SSH and ADB network forwarding | Diagnostics / network | Fake remote + D7 network acceptance |
| `canfd-link-diag` | SocketCAN probe and configuration | Diagnostics / SocketCAN | Multi-version `ip` fixtures + Orin |
| `canfd-link-diag` | Classic/FD quick, standard, long and custom stress tests | Diagnostics / stress | Profile fixtures + bus fault injection |
| `canfd-link-diag` | dmesg, candump, controller counters and fault classification | Diagnostics / evidence | Parser fixtures + injected bus-off |
| `canfd-link-diag` | Node parameters and broadcast response accounting | Diagnostics / nodes | EVT-driven fixtures + robot acceptance |
| `canfd-link-diag` | Bit timing and mttcan TDC/TDCR | Diagnostics / timing | Golden calculations + Orin read-back |
| `canfd-link-diag` | HTML, JSON and raw evidence reports | Logs / reports | Self-contained HTML + schema tests |
| New | Read-only `/sdcard/pudu/log` catalog and verified download | Logs / device logs | Path/symlink/stat/hash tests + Orin |
| `actuator_sdk` | Position mode, enable/disable, clear fault and state | Motor / CAN motion | SDK/unit + vcan + motor bench |
| `actuator_sdk` | Velocity mode with ±14 encoding, 1000 ms default acceleration | Motor / CAN motion | Golden frame + staged ±0.5 bench |
| `actuator_sdk` | Fixed-group position and velocity | Motor / CAN motion | Partial-failure safety test + group bench |
| `actuator_sdk` | Parameter read/write/save and zero calibration | Motor / parameters/zero | Two-phase and read-back tests |

D9, D5W and D5 automatic calibration paths are explicitly outside this product and must not be loaded as fallbacks.


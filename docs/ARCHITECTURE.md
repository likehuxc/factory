# D7 Factory Studio architecture

## Dependency direction

```text
ui -> application coordinator -> feature services -> transport ports
                                      |
                                      -> pure protocols

EVT YAML -> validated EvtConfig -> UI + services + diagnostics
```

- `core` contains UI-independent models, cancellation primitives, EVT validation and transport contracts.
- `protocols` owns byte-level frame encoding and decoding. It must not import Qt, ctypes, serial or SSH libraries.
- `transports` owns ZLG, serial and remote I/O resources.
- `features` owns workflows and safety cleanup around transports.
- `ui` submits named actions and renders state. It never reports hardware success before a service returns it.
- `reports` owns portable result bundles and must redact secrets.

## Safety invariants

- The motor session starts locked and re-locks after faults, disconnects, connection-mode changes, interface changes and EVT changes.
- A mode change disables the target, changes and verifies the mode, and leaves the target disabled.
- Cancelling motion performs a bounded zero-speed then disable sequence.
- Diagnostic cancellation terminates the entire remote process group.
- Device log code has no remote delete capability.
- D7 configuration loading rejects other robot models and duplicate logic/device IDs.


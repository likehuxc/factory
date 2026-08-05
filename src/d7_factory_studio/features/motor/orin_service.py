from __future__ import annotations

from typing import Any, Protocol


class AgentRequester(Protocol):
    def request(
        self,
        operation: str,
        *,
        target: dict[str, Any] | None = None,
        args: dict[str, Any] | None = None,
        timeout_s: float = 15.0,
        request_id: str | None = None,
    ) -> dict[str, Any]: ...


class OrinMotorService:
    MAX_FACTORY_VELOCITY_RAD_S = 0.5

    def __init__(self, client: AgentRequester) -> None:
        self.client = client
        self._zero_tokens: dict[tuple[str, tuple[Any, ...]], str] = {}

    @staticmethod
    def normalize_target(target: dict[str, Any]) -> dict[str, list[Any]]:
        if set(target) == {"motor"}:
            return {"motors": [int(target["motor"])]}
        if set(target) == {"group"}:
            return {"groups": [str(target["group"])]}
        if "motors" in target:
            ids = [int(value) for value in target["motors"]]
            if not ids:
                raise ValueError("目标电机不能为空")
            return {"motors": ids}
        raise ValueError("只支持单电机或固定分组目标")

    def enable(self, target: dict[str, Any]) -> None:
        self.client.request("motor.enable", target=self.normalize_target(target))

    def disable(self, target: dict[str, Any]) -> None:
        self.client.request("motor.disable", target=self.normalize_target(target))

    def clear_errors(self, target: dict[str, Any]) -> None:
        self.client.request("motor.clear_errors", target=self.normalize_target(target))

    def set_control_authority(self, target: dict[str, Any], owned: bool) -> None:
        self.client.request(
            "motor.set_control_authority",
            target=self.normalize_target(target),
            args={"owned": bool(owned)},
        )

    def set_mode(self, target: dict[str, Any], mode: str) -> None:
        if mode not in {"position", "velocity"}:
            raise ValueError("模式必须是 position 或 velocity")
        self.client.request("motor.set_mode", target=self.normalize_target(target), args={"mode": mode})

    def set_position(self, target: dict[str, Any], angle_deg: float) -> None:
        radians = float(angle_deg) * 3.141592653589793 / 180.0
        self.client.request("motor.set_position", target=self.normalize_target(target), args={"rad": radians})

    def set_velocity(self, target: dict[str, Any], rad_s: float, accel_time_ms: int = 1000) -> None:
        velocity = float(rad_s)
        if abs(velocity) > self.MAX_FACTORY_VELOCITY_RAD_S:
            raise ValueError("首轮台架速度必须在 ±0.5 rad/s")
        acceleration = int(accel_time_ms)
        if not 1 <= acceleration <= 65535:
            raise ValueError("加速时间必须在 1..65535 ms")
        self.client.request(
            "motor.set_velocity",
            target=self.normalize_target(target),
            args={"rad_s": velocity, "accel_time_ms": acceleration},
        )

    def snapshot(self, target: dict[str, Any]) -> dict[str, Any]:
        return self.client.request("state.snapshot", target=self.normalize_target(target))

    def zero_prepare(self, target: dict[str, Any]) -> str:
        normalized = self.normalize_target(target)
        response = self.client.request("zero.prepare", target=normalized)
        token = str(response["token"])
        self._zero_tokens[self._target_key(normalized)] = token
        return token

    def zero_commit(self, target: dict[str, Any]) -> None:
        normalized = self.normalize_target(target)
        token = self._zero_tokens.pop(self._target_key(normalized), None)
        if not token:
            raise RuntimeError("缺少有效的零位准备令牌")
        self.client.request("zero.commit", args={"token": token})

    @staticmethod
    def _target_key(normalized: dict[str, list[Any]]) -> tuple[str, tuple[Any, ...]]:
        if "motors" in normalized:
            return "motors", tuple(normalized["motors"])
        return "groups", tuple(normalized["groups"])

    def emergency_stop(self, target: dict[str, Any]) -> None:
        # Explicit zero velocity precedes disable; agent also repeats this on EOF/cancel.
        normalized = self.normalize_target(target)
        self.client.request(
            "motor.set_velocity", target=normalized, args={"rad_s": 0.0, "accel_time_ms": 1000}
        )
        self.client.request("motor.disable", target=normalized)

"""华为 ``electric`` 服务：把用电统计特性映射到 Home Assistant 数值实体。

全量 5,872 个 Profile 里 **71 个** 使用 serviceId=electric，标量特性只有 9 个：

    current            int    电流（单位可能是 ``mA``，需要 scale）
    day                float  当日用电（kWh）
    totalConsum        int    总用电量（kWh）
    totalElectricity   float  总用电量（kWh，step 0.01）
    totalConsumption   float  总用电量（kWh）
    voltage            int    电压（单位可能是 ``V``、也可能是 0.1 V）
    hour               float  小时用电（kWh）
    saveConsum         int    节约电量
    battery            int    电量百分比（1~100）
    extNum / circuitsNum  int 多路计量配置（仅 2 台，与能耗无关）

其余 30+ 个特性是 ``array[object]``（多相电表的 ``realTimeVoltageA/B/C``、
``circuitName1..10``、``showElectricity1/2``、``realTimeWattage`` 等），
单个标量实体表达不了，本适配器不支持。

因此适配器不绑定具体特性名，而是由 ``fields`` 声明「这次要报哪几个特性」，
按实体绑定顺序一一对应；``integers`` 标记需要按整数上报的特性，
``scales`` 给出每个特性的换算系数（如 HA 侧是 ``A``、特性单位是 ``mA`` 时取 1000）。

默认契约来源
    prodId   : ``9MTX`` —— 断路器，``electric`` 为
               ``totalConsum`` / ``currentElectric1`` / ``currentVoltage1`` /
               ``currentPower1`` 四个 float
    Profile  : https://smarthome-drcn.dbankcdn.com/device/guide/9MTX/9MTX.json
    常见性   : 71 个 Profile；其中 31 个是 WiFi（可注册），64 个含至少一个数值特性
               （墙面插座 11、转换插座 7、洗衣机 3、挂机空调 2、电表 2、
                智能插排 1、空调伴侣 1、智能插座 1、中央空调 1、柜机空调 1、断路器 1）
    已验证   : 2026-10-26，6 个不同 Profile 各注册一台子设备、绑真实 HA 实体实测：
               - ``2HRE``（三个 int，``current`` 单位 mA，``{current: 1000}``）
                 电流 2125 / 电压 225 / 总电量 153
               - ``2KD9``（三个 int，全无单位）电流 2 / 电压 226 / 总电量 2653
               - ``2RE4``（单 float ``totalConsumption``）正常上报
               - ``2OYZ``（单 float ``day``）正常上报
               - ``2MFF``（5 字段混合 int + float，含 mA）5 个字段全部上报
               - ``9MTX``（4 字段全 float，kWh/A/V/W）5961.9 / 1.84 / 225.3 / 137.9
               6 台均满足：注册成功、``initial dataSync accepted``、
               云端回存值与上报值一致、App 卡片显示正常
    未验证   : ``array[object]`` 字段（多回路电表）、``enum`` / ``string`` 字段、
               ``RW`` 字段下写（全量仅 2 个）、单位自动推断（由 ``scales`` 显式配置）

contract
    fields  : 特性名列表，顺序与 ``bindings`` 里的实体顺序一致（必填）
    integers: 需要按整数上报的特性名（可选；多数 ``int`` 特性应列在这里）
    scales  : 特性名 -> 换算系数（可选；缺省 1.0）。
              同一特性不能既在 ``integers`` 里、又用小于 1 的 scale
              （会四舍五入成 0，加载期报错）

绑定的实体必须是 ``sensor`` / ``number`` / ``input_number``，状态按数值解析；
``unknown`` / ``unavailable`` / 非数字都会跳过（非数字只告警一次）。

示例
    services:
    - st: electric
      sid: electric
      contract:
        fields: [totalConsum, current, voltage]
        integers: [totalConsum, current, voltage]
        scales: {current: 0.001}
    bindings:
    - sid: electric
      entities: [sensor.pzem_energy, sensor.pzem_current, sensor.pzem_voltage]
"""

from __future__ import annotations

import logging

from ..models import ServiceRef


_LOGGER = logging.getLogger(__name__)

_WARNED: set[tuple[str, str, object]] = set()

# 只接受数值状态实体
_NUMERIC_DOMAINS = ("sensor", "number", "input_number")


class ElectricServiceAdapter:
    service_id = "electric"

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != self.service_id:
            raise ValueError(f"{self.service_id} requires st: {self.service_id}")
        unknown = set(dict(service.contract)) - {"fields", "integers", "scales"}
        if unknown:
            raise ValueError(
                f"{self.service_id} has unsupported contract keys: "
                + ", ".join(sorted(unknown))
            )
        _contract(service, self.service_id)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if not entity_ids:
            raise ValueError("electric requires at least one HA entity")
        for entity_id in entity_ids:
            domain = entity_id.partition(".")[0]
            if domain not in _NUMERIC_DOMAINS:
                raise ValueError(
                    f"electric binds numeric entities only; got {entity_id!r}"
                )

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        fields, integers, scales = _contract(service, self.service_id)
        if len(entity_ids) != len(fields):
            _warn_once(
                (self.service_id, "count", len(entity_ids)),
                "%s: %d entities bound, %d fields declared; report skipped"
                % (self.service_id, len(entity_ids), len(fields)),
            )
            return {}
        payload: dict[str, object] = {}
        for field, entity_id in zip(fields, entity_ids):
            state = hass.states.get(entity_id)
            if state is None or state.state in {"unknown", "unavailable"}:
                continue
            try:
                value = float(state.state) * scales.get(field, 1.0)
            except (TypeError, ValueError):
                _warn_once(
                    (self.service_id, entity_id, field),
                    "%s: %s is not a number for %s"
                    % (self.service_id, entity_id, field),
                )
                continue
            payload[field] = int(round(value)) if field in integers else round(value, 4)
        return payload

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        # electric 的特性在 Profile 里都是只读（R），没有下行命令可执行。
        return


ADAPTER = ElectricServiceAdapter()


def _contract(
    service: ServiceRef, prefix: str = "electric"
) -> tuple[list[str], set[str], dict[str, float]]:
    """解析并校验 ``fields`` / ``integers`` / ``scales``（``prefix`` 用于报错文案）。"""

    raw_fields = service.contract_value("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ValueError(prefix + " contract fields must be a non-empty list")
    fields: list[str] = []
    for name in raw_fields:
        if not isinstance(name, str) or not name:
            raise ValueError(prefix + " contract fields must be non-empty text")
        if name in fields:
            raise ValueError(f"{prefix} contract fields repeats {name!r}")
        fields.append(name)

    raw_integers = service.contract_value("integers")
    if raw_integers is None:
        integers: set[str] = set()
    elif not isinstance(raw_integers, list) or not raw_integers:
        raise ValueError(prefix + " contract integers must be a non-empty list")
    else:
        integers = set()
        for name in raw_integers:
            if not isinstance(name, str) or name not in fields:
                raise ValueError(
                    prefix + " contract integers must list names from fields"
                )
            integers.add(name)

    raw_scales = service.contract_value("scales")
    if raw_scales is None:
        scales: dict[str, float] = {}
    elif not isinstance(raw_scales, dict) or not raw_scales:
        raise ValueError(prefix + " contract scales must be a non-empty mapping")
    else:
        scales = {}
        for name, value in raw_scales.items():
            if not isinstance(name, str) or name not in fields:
                raise ValueError(prefix + " contract scales must list names from fields")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(prefix + " contract scales values must be numbers")
            scales[name] = float(value)
    for name in integers & set(scales):
        if scales[name] < 1:
            raise ValueError(
                f"{prefix} contract {name!r} is both integer and scaled below 1, "
                "which would round to 0; drop it from integers"
            )
    return fields, integers, scales


def _warn_once(key: tuple[str, str, object], message: str) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    _LOGGER.warning(message)

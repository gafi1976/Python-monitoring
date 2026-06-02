"""Device model for Network Map application"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DeviceStatus(Enum):
    ONLINE   = "Online"
    OFFLINE  = "Offline"
    UNKNOWN  = "Неизвестно"
    CHECKING = "Проверка..."


@dataclass
class Device:
    dev_id:        str
    name:          str            = "Устройство"
    ip:            str            = "0.0.0.0"
    dtype:         str            = "other"       # router/switch/server/pc/printer/camera/phone/ups/other
    x:             float          = 100.0
    y:             float          = 100.0

    # Check settings
    ping_enabled:  bool           = True
    snmp_enabled:  bool           = False
    snmp_community:str            = "public"
    snmp_port:     int            = 161
    snmp_version:  str            = "2c"

    # Список OID с поддержкой коэффициента (factor), триггеров

    snmp_lld_rules: list = field(default_factory=lambda: [])
    snmp_oids:     list           = field(default_factory=lambda: [
    {"label": "Описание", "oid": "1.3.6.1.2.1.1.1.0", "unit": "", "factor": 1.0, "condition": "", "threshold": "", "enabled": True, "last_alert": 0},
        {"label": "Uptime", "oid": "1.3.6.1.2.1.1.3.0", "unit": "", "factor": 1.0, "condition": "", "threshold": "", "enabled": True, "last_alert": 0}
        ])

    
    check_interval:int            = 30            # Интервал проверки в секундах

    # Runtime state
    status:        DeviceStatus   = DeviceStatus.UNKNOWN
    latency:       Optional[float] = None
    last_checked:  Optional[str]  = None
    snmp_last_info: Optional[dict] = None


    # Extra
    description:   str            = ""
    location:      str            = ""

    def to_dict(self) -> dict:
        return {
            "dev_id":        self.dev_id,
            "name":          self.name,
            "ip":            self.ip,
            "dtype":         self.dtype,
            "x":             self.x,
            "y":             self.y,
            "ping_enabled":  self.ping_enabled,
            "snmp_enabled":  self.snmp_enabled,
            "snmp_community":self.snmp_community,
            "snmp_port":     self.snmp_port,
            "snmp_version":  self.snmp_version,
            "snmp_oids":     self.snmp_oids,
            "check_interval":self.check_interval,
            "description":   self.description,
            "location":      self.location,
            "snmp_lld_rules": self.snmp_lld_rules,
        }

    @staticmethod
    def from_dict(data: dict) -> "Device":
        dev = Device(dev_id=data["dev_id"])
        dev.name           = data.get("name", "Устройство")
        dev.ip             = data.get("ip", "0.0.0.0")
        dev.dtype          = data.get("dtype", "other")
        dev.x              = data.get("x", 100.0)
        dev.y              = data.get("y", 100.0)
        dev.ping_enabled   = data.get("ping_enabled", True)
        dev.snmp_enabled   = data.get("snmp_enabled", False)
        dev.snmp_community = data.get("snmp_community", "public")
        dev.snmp_port      = data.get("snmp_port", 161)
        dev.snmp_version   = data.get("snmp_version", "2c")
        dev.snmp_oids      = data.get("snmp_oids", [])
        dev.snmp_lld_rules = data.get("snmp_lld_rules", [])

        # Нормализация для обратной совместимости
        for oid in dev.snmp_oids:
            oid.setdefault("unit", "")
            oid.setdefault("factor", 1.0)
            oid.setdefault("condition", "")
            oid.setdefault("threshold", "")
            oid.setdefault("enabled", True)
            oid.setdefault("last_alert", 0)
        dev.check_interval = data.get("check_interval", 30)
        dev.description    = data.get("description", "")
        dev.location       = data.get("location", "")
        return dev
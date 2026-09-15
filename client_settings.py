"""UI-neutral application settings model.

The model intentionally contains only user preferences that can be applied
without owning Qt widgets.  Qt presentation code consumes this immutable value
and the persistence layer serializes it through the common v1 envelope.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ClientSettings:
    font_family: str = "Consolas"
    font_size: int = 10
    output_foreground: str = "#e5e5e5"
    output_background: str = "#000000"
    scrollback_blocks: int = 10_000
    timestamps: bool = False
    local_echo: bool = True
    default_auto_reconnect: bool = True
    default_reconnect_base_delay: float = 3.0
    default_reconnect_max_delay: float = 60.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ClientSettings":
        if not isinstance(data, dict):
            raise ValueError("settings data must be an object")

        allowed = set(cls.__dataclass_fields__)
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown settings field(s): {', '.join(sorted(unknown))}")

        values = cls().to_dict()
        values.update(data)

        font_family = values["font_family"]
        if not isinstance(font_family, str) or not font_family.strip():
            raise ValueError("font_family must be a non-empty string")
        if len(font_family) > 256:
            raise ValueError("font_family is too long")

        font_size = values["font_size"]
        if type(font_size) is not int or not 6 <= font_size <= 72:
            raise ValueError("font_size must be an integer from 6 to 72")

        for field in ("output_foreground", "output_background"):
            value = values[field]
            if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
                raise ValueError(f"{field} must be a #RRGGBB color")
            try:
                int(value[1:], 16)
            except ValueError as exc:
                raise ValueError(f"{field} must be a #RRGGBB color") from exc
            values[field] = value.lower()

        blocks = values["scrollback_blocks"]
        if type(blocks) is not int or not 100 <= blocks <= 1_000_000:
            raise ValueError("scrollback_blocks must be an integer from 100 to 1000000")

        for field in ("timestamps", "local_echo", "default_auto_reconnect"):
            if type(values[field]) is not bool:
                raise ValueError(f"{field} must be true or false")

        base = values["default_reconnect_base_delay"]
        maximum = values["default_reconnect_max_delay"]
        if type(base) not in (int, float) or not 0.1 <= float(base) <= 3600.0:
            raise ValueError("default_reconnect_base_delay must be from 0.1 to 3600 seconds")
        if type(maximum) not in (int, float) or not 0.1 <= float(maximum) <= 3600.0:
            raise ValueError("default_reconnect_max_delay must be from 0.1 to 3600 seconds")
        if float(maximum) < float(base):
            raise ValueError("default reconnect maximum must be at least the base delay")
        values["default_reconnect_base_delay"] = float(base)
        values["default_reconnect_max_delay"] = float(maximum)
        values["font_family"] = font_family.strip()

        return cls(**values)

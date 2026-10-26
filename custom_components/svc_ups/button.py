"""Button platform for SVC Richcomm UPS (self-test and beeper controls)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SvcUpsConfigEntry, SvcUpsCoordinator
from .entity import SvcUpsEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class SvcUpsButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[SvcUpsCoordinator], Awaitable[None]]


BUTTON_TYPES: tuple[SvcUpsButtonDescription, ...] = (
    SvcUpsButtonDescription(
        key="start_self_test",
        translation_key="start_self_test",
        press_fn=lambda c: c.async_start_self_test(),
    ),
    SvcUpsButtonDescription(
        key="cancel_self_test",
        translation_key="cancel_self_test",
        press_fn=lambda c: c.async_cancel_self_test(),
    ),
    SvcUpsButtonDescription(
        key="toggle_beeper",
        translation_key="toggle_beeper",
        press_fn=lambda c: c.async_toggle_beeper(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SvcUpsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание кнопок управления ИБП."""
    async_add_entities(SvcUpsButton(entry.runtime_data, d) for d in BUTTON_TYPES)


class SvcUpsButton(SvcUpsEntity, ButtonEntity):
    entity_description: SvcUpsButtonDescription

    def __init__(self, coordinator: SvcUpsCoordinator, description: SvcUpsButtonDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.coordinator)

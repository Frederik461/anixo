"""Pure navigation helpers shared by screens."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Selection:
    """Clamped index and page state. No UI or rendering side effects."""

    index: int = 0
    page_size: int = 12

    def clamp(self, item_count: int) -> int:
        self.page_size = max(1, self.page_size)
        if item_count <= 0:
            self.index = 0
        else:
            self.index = max(0, min(self.index, item_count - 1))
        return self.index

    def move(self, delta: int, item_count: int) -> int:
        self.clamp(item_count)
        if item_count:
            self.index = max(0, min(item_count - 1, self.index + delta))
        return self.index

    @property
    def page_start(self) -> int:
        return (self.index // self.page_size) * self.page_size

    @property
    def page_end(self) -> int:
        return self.page_start + self.page_size

    def page_move(self, delta: int, item_count: int) -> int:
        self.clamp(item_count)
        if item_count:
            self.index = max(0, min(item_count - 1, self.index + delta * self.page_size))
        return self.index

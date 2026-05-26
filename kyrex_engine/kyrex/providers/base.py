from abc import ABC, abstractmethod
from typing import Optional


class BaseProvider(ABC):
    @abstractmethod
    async def chat(self, model: str, messages: list, tools: Optional[list] = None, stream_callback=None, reasoning_callback=None) -> dict:
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    def supports_reasoning(self) -> bool:
        return False

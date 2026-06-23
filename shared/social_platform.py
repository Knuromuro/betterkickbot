from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PlatformActionResult:
    ok: bool
    status: str
    code: str
    action: str
    account_id: str
    channel: str
    event_id: str | None = None
    retry_after: int | None = None
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class SocialPlatformAdapter(ABC):
    @abstractmethod
    def send_message(
        self, account_id: str, channel: str, message: str
    ) -> PlatformActionResult:
        raise NotImplementedError

    @abstractmethod
    def follow_channel(self, account_id: str, channel: str) -> PlatformActionResult:
        raise NotImplementedError

    @abstractmethod
    def unfollow_channel(self, account_id: str, channel: str) -> PlatformActionResult:
        raise NotImplementedError

    @abstractmethod
    def get_user(self, account_id: str) -> dict | None:
        raise NotImplementedError

    @abstractmethod
    def get_channel(self, channel: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def get_followers(self, channel: str) -> list[str]:
        raise NotImplementedError

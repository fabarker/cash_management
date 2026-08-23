from abc import ABC, abstractmethod
from typing import List, Generic, TypeVar

RefsT = TypeVar('RefsT')
StateT = TypeVar('StateT')

class BaseController(ABC, Generic[RefsT, StateT]):
    def __init__(self, refs: RefsT, state: StateT):
        self.refs = refs
        self.state = state

    @abstractmethod
    def render(self) -> str:
        pass

    @abstractmethod
    def update_state(self, new_state: StateT) -> None:
        pass

    @abstractmethod
    def handle_event(self, event: str) -> None:
        pass
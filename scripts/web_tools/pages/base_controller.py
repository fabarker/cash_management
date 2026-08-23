from abc import ABC, abstractmethod
from typing import Generic, TypeVar

RefsT = TypeVar('RefsT')
StateT = TypeVar('StateT')

class BaseController(ABC, Generic[RefsT, StateT]):
    def __init__(self, *, refs: RefsT, state: StateT) -> None:
        self.refs = refs
        self.state = state

    @abstractmethod
    def wire(self) -> None:
        raise NotImplementedError


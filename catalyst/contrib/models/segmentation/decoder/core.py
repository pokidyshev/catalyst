from typing import List  # isort:skip
from abc import ABC, abstractmethod

import torch
import torch.nn as nn


class DecoderSpec(ABC, nn.Module):
    def __init__(self, in_channels: List[int], in_strides: List[int]):
        super().__init__()
        self.in_channels = in_channels
        self.in_strides = in_strides

    @property
    @abstractmethod
    def out_channels(self) -> List[int]:
        pass

    @property
    @abstractmethod
    def out_strides(self) -> List[int]:
        pass

    @abstractmethod
    def forward(self, x: List[torch.Tensor]) -> torch.Tensor:
        pass

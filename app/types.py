"""Shared primitive types for the optimizer API contract."""

from typing import Annotated

from pydantic import Field


AbilityLevel = Annotated[int, Field(ge=1, le=5)]
Weekday = Annotated[int, Field(ge=0, le=6)]

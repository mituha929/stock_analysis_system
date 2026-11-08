from pydantic import Field, model_validator

from app.rules.schemas import StrictModel
from app.schemas.screener import ScreenerRequest


class SavedScreenerCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    configuration: ScreenerRequest

    @model_validator(mode="after")
    def clean_name(self):
        self.name = self.name.strip()
        self.description = self.description.strip()
        if not self.name:
            raise ValueError("策略名稱不可空白")
        return self


class SavedScreenerUpdate(SavedScreenerCreate):
    expected_revision: int = Field(ge=1, strict=True)

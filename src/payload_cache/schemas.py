"""API contracts, shared by the service and the CLI so both validate the same way."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PayloadRequest(BaseModel):
    # Unknown fields are rejected rather than ignored: a misspelt key would otherwise
    # produce a confusing validation error about the field that was *not* sent.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "list_1": ["first string", "second string", "third string"],
                "list_2": ["other string", "another string", "last string"],
            }
        },
    )

    list_1: list[str] = Field(min_length=1)
    list_2: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _lists_have_same_length(self) -> "PayloadRequest":
        if len(self.list_1) != len(self.list_2):
            raise ValueError(
                "list_1 and list_2 must have the same length "
                f"({len(self.list_1)} != {len(self.list_2)})"
            )
        return self


class PayloadCreated(BaseModel):
    id: str
    message: str


class PayloadRead(BaseModel):
    output: str

"""API contracts, shared by the service and the CLI so both validate the same way."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

# Upper bounds so that one request cannot queue an unbounded number of transformer calls or
# fill the cache with megabyte-sized rows. Module constants rather than settings: Pydantic
# constraints are static, and the CLI must validate exactly like the server.
MAX_ITEMS = 1_000
MAX_ITEM_LENGTH = 1_000


def _storable_text(value: str) -> str:
    """Reject a string the database cannot store.

    NUL is legal in JSON but PostgreSQL refuses it in text columns; catching it here turns a
    500 from the database into a 422 on every backend. (Lone surrogates, the other legal
    JSON that is not valid text, are already refused by Pydantic's string validation.)
    """
    if "\x00" in value:
        raise ValueError("must not contain NUL characters")
    return value


Item = Annotated[str, Field(max_length=MAX_ITEM_LENGTH), AfterValidator(_storable_text)]


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

    list_1: list[Item] = Field(min_length=1, max_length=MAX_ITEMS)
    list_2: list[Item] = Field(min_length=1, max_length=MAX_ITEMS)

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

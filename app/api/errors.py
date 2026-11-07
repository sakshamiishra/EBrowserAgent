"""Safe API error formatting that does not echo submitted values."""

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


async def safe_request_validation_error(
    _request: object, _exc: RequestValidationError
) -> JSONResponse:
    """Return a generic validation message without Pydantic's input values."""

    return JSONResponse(
        status_code=422,
        content={
            "detail": "Request validation failed. Check the URL, instruction, and test-data limits."
        },
    )

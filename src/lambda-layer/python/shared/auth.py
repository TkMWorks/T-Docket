"""Shared helper for reading the caller's identity out of a verified JWT.

Every handler in this project builds its DynamoDB keys and S3 object
keys from this value alone -- never from anything in the request body
or path -- so that ownership is enforced structurally rather than by
a conditional check someone could forget to write.
"""


class AuthError(Exception):
    """Raised when the request has no usable, verified identity."""


def get_user_sub(event: dict) -> str:
    """Extracts the Cognito 'sub' claim from a verified JWT.

    Assumes API Gateway HTTP API (payload format 2.0) with a Cognito
    JWT authorizer attached to the route. API Gateway verifies the
    token's signature and expiry before this Lambda ever runs -- this
    function only reads a claim out of an already-trusted request
    context, it does not itself perform any verification.

    For a REST API with a Cognito user pool authorizer instead of an
    HTTP API JWT authorizer, the claims live one level up, at
    event["requestContext"]["authorizer"]["claims"] (no "jwt" key).
    """
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        sub = claims["sub"]
    except (KeyError, TypeError) as exc:
        raise AuthError("Missing or unverified JWT claims") from exc

    if not sub or not isinstance(sub, str):
        raise AuthError("Invalid sub claim")

    return sub
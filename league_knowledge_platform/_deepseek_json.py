"""Private bounded JSON transport shared by independent semantic adapters; no retries."""

import json


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys")
        result[key] = value
    return result


def invalid_constant(_):
    raise ValueError("Non-finite JSON constant")


def strict_json(content):
    try:
        return json.loads(content, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except RecursionError:
        raise ValueError("JSON nesting exceeds the parser's supported depth") from None


def read_bounded(response, limit, error):
    content = bytearray()
    for chunk in response.iter_content(chunk_size=4096):
        if len(content) + len(chunk) > limit:
            raise error("INVALID_OUTPUT", True)
        content.extend(chunk)
    return bytes(content)


def http_failure(status, content, error):
    if status in (408, 429, 500, 502, 503, 504):
        kind = "TIMEOUT" if status == 408 else "RATE_LIMIT" if status == 429 else "SERVICE"
        raise error(kind, True)
    if status in (400, 413, 422):
        code = None
        try:
            document = strict_json(content.decode("utf-8"))
            code = document.get("error", {}).get("code")
        except (ValueError, AttributeError, UnicodeError):
            pass
        kind = "CONTEXT_LIMIT" if code in ("context_length_exceeded", "context_limit_exceeded") else "INVALID_REQUEST"
    else:
        kind = {401: "AUTHENTICATION", 402: "BALANCE", 403: "AUTHORIZATION"}.get(status, "PROVIDER_CONTRACT")
    raise error(kind, False)


def complete_json(session, url, body, api_key, timeout, *, read, on_http_failure,
                  error, length_kind="INVALID_OUTPUT"):
    # Callers retain session/key lifecycle, payload projection and exception mapping.
    with session.post(url, json=body, headers={"Authorization": "Bearer " + api_key},
                      timeout=timeout, allow_redirects=False, stream=True) as response:
        content = read(response)
        if response.status_code != 200:
            on_http_failure(response.status_code, content)
    document = strict_json(content.decode("utf-8"))
    choices = document["choices"]
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("Expected one completed response")
    choice = choices[0]
    finish = choice["finish_reason"]
    if finish in ("aborted", "insufficient_system_resource"):
        raise error("SERVICE", True)
    if finish == "content_filter":
        raise error("PROVIDER_CONTRACT", False)
    if finish != "stop":
        raise error(length_kind if finish == "length" else "INVALID_OUTPUT", True)
    message = choice["message"]
    if message["role"] != "assistant" or message.get("tool_calls") or message.get("reasoning_content"):
        raise error("PROVIDER_CONTRACT", False)
    if not isinstance(message["content"], str) or not message["content"].strip():
        raise error("INVALID_OUTPUT", True)
    return strict_json(message["content"]), document.get("model")

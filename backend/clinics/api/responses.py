from rest_framework.renderers import JSONRenderer


class EnvelopeJSONRenderer(JSONRenderer):
    """Wrap all successful API results in a stable data envelope."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        response = (renderer_context or {}).get("response")
        if response is not None and response.status_code >= 400:
            return super().render(data, accepted_media_type, renderer_context)
        if not (isinstance(data, dict) and set(data).issubset({"data", "meta"}) and "data" in data):
            data = {"data": data}
        return super().render(data, accepted_media_type, renderer_context)


def success(data, *, meta=None, status=200):
    from rest_framework.response import Response

    body = {"data": data}
    if meta is not None:
        body["meta"] = meta
    return Response(body, status=status)

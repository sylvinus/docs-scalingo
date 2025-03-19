"""
Django middlewares
"""

import os
import re

from django.conf import settings
from django.http import HttpResponse


class StaticRewritesMiddleware:
    """
    Middleware to serve the SPA's index.html for all non-static/media/API routes
    """

    def __init__(self, get_response):
        self.get_response = get_response
        # Compile regex patterns for paths we want to handle
        self.docs_pattern = re.compile(r"^/docs/[\w-]+/?$")

    def __call__(self, request):
        # First try the normal response
        response = self.get_response(request)

        # If it's a 404 and matches our patterns, serve index.html
        if response.status_code == 404:
            path = request.path_info

            # Handle /docs/xyz paths
            if self.docs_pattern.match(path):
                try:
                    with open(
                        os.path.join(settings.STATIC_ROOT, "docs/[id]/index.html"), "rb"
                    ) as f:
                        return HttpResponse(f.read(), content_type="text/html")
                except FileNotFoundError:
                    pass

        return response

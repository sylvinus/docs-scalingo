"""
Django middlewares
"""

import logging
import os
import re

from django.conf import settings
from django.core.files.storage import default_storage
from django.http import HttpResponse, StreamingHttpResponse

from botocore.exceptions import ClientError

from core.api.viewsets import MEDIA_STORAGE_URL_PATTERN
from core.models import Document

logger = logging.getLogger(__name__)


class NativeProxyMiddleware:
    """
    Middleware that replaces nginx proxying of:
     - the SPA's docs/[id]/index.html
     - the media files on /media/*
    """

    def __init__(self, get_response):
        self.get_response = get_response
        # Compile regex patterns for paths we want to handle
        self.docs_pattern = re.compile(r"^/docs/[\w-]+/?$")
        self.media_pattern = MEDIA_STORAGE_URL_PATTERN

    def __call__(self, request):
        # First try the normal response
        response = self.get_response(request)

        # If it's a 404 and matches our patterns, check for rewrites
        if response.status_code == 404:
            path = request.path_info

            # Handle /docs/xyz paths
            if self.docs_pattern.match(path):
                docs_response = self.serve_docs()
                if docs_response:
                    return docs_response

            # Handle media files on /media/*
            media_match = self.media_pattern.search(path)
            if media_match:
                return self.serve_media(request, media_match)

        return response

    def serve_docs(self):
        """
        Serve the single page application for document routes.
        """
        try:
            with open(
                os.path.join(settings.STATIC_ROOT, "docs/[id]/index.html"), "rb"
            ) as f:
                return HttpResponse(f.read(), content_type="text/html")
        except FileNotFoundError:
            return None

    def serve_media(self, request, media_match):
        """
        Serve media files from object storage with access control.
        """
        # Get document ID and file path from the URL
        document_id = media_match.group("pk")
        file_path = media_match.group("key")

        # First check if the user has permission to access this document
        try:
            # Try to get the document
            document = Document.objects.get(pk=document_id)

            # Check if the user has access to this document
            user_abilities = document.get_abilities(request.user)
            if not user_abilities.get("media_auth", False):
                logger.warning(
                    "Access denied: User %s attempted to access file %s for document %s",
                    request.user,
                    file_path,
                    document_id,
                )
                return HttpResponse(content="Access denied", status=403)

        except Document.DoesNotExist:
            logger.warning("Document %s not found for media access", document_id)
            return HttpResponse(content="Document not found", status=404)

        # Construct the full storage key including document ID
        full_key = f"{document_id}/{file_path}"

        try:
            # Use boto3 to get object metadata
            obj_metadata = default_storage.connection.meta.client.head_object(
                Bucket=default_storage.bucket_name, Key=full_key
            )

            # Get content type from metadata
            content_type = obj_metadata.get("ContentType", "application/octet-stream")

            # Create a streaming response using Boto3's streaming capability
            obj = default_storage.connection.meta.client.get_object(
                Bucket=default_storage.bucket_name, Key=full_key
            )

            # Stream the file content efficiently
            body_stream = obj["Body"]

            # Copy over content disposition if present
            response_kwargs = {
                "content_type": content_type,
            }

            if content_disposition := obj_metadata.get("ContentDisposition"):
                response_kwargs["headers"] = {
                    "Content-Disposition": content_disposition
                }

            return StreamingHttpResponse(
                streaming_content=body_stream, **response_kwargs
            )

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")

            if error_code in {"404", "NoSuchKey"}:
                # File doesn't exist, keep the 404 response
                logger.info("File not found in storage: %s", full_key)
                return HttpResponse(content="File not found", status=404)

            # Log the detailed error for debugging
            logger.error("S3 error accessing %s: %s", full_key, str(e))
            return HttpResponse(content="Error accessing file storage", status=500)

        # We use a specific exception for ClientError above,
        # but need to handle other unexpected errors
        # pylint: disable=broad-exception-caught
        except Exception as e:
            # Catch any other unexpected errors
            logger.exception("Unexpected error serving file %s: %s", full_key, str(e))
            return HttpResponse(content="Internal server error", status=500)

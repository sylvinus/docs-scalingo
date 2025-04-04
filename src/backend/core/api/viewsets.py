"""API endpoints"""
# pylint: disable=too-many-lines

import datetime
import logging
import re
import uuid
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.postgres.aggregates import ArrayAgg
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.search import TrigramSimilarity
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import models as db
from django.db import transaction
from django.db.models.expressions import RawSQL
from django.db.models.functions import Left, Length
from django.http import Http404

import jwt
import rest_framework as drf
from botocore.exceptions import ClientError
from django_filters import rest_framework as drf_filters
from rest_framework import filters, status, viewsets
from rest_framework import response as drf_response
from rest_framework.permissions import AllowAny

from core import authentication, enums, models
from core.services.ai_services import AIService
from core.services.collaboration_services import CollaborationService

from . import permissions, serializers, utils
from .filters import DocumentFilter

logger = logging.getLogger(__name__)

ATTACHMENTS_FOLDER = "attachments"
UUID_REGEX = (
    r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}"
)
FILE_EXT_REGEX = r"\.[a-zA-Z0-9]{1,10}"
MEDIA_STORAGE_URL_PATTERN = re.compile(
    f"{settings.MEDIA_URL:s}(?P<pk>{UUID_REGEX:s})/"
    f"(?P<key>{ATTACHMENTS_FOLDER:s}/{UUID_REGEX:s}(?:-unsafe)?{FILE_EXT_REGEX:s})$"
)
COLLABORATION_WS_URL_PATTERN = re.compile(rf"(?:^|&)room=(?P<pk>{UUID_REGEX})(?:&|$)")

# pylint: disable=too-many-ancestors


class NestedGenericViewSet(viewsets.GenericViewSet):
    """
    A generic Viewset aims to be used in a nested route context.
    e.g: `/api/v1.0/resource_1/<resource_1_pk>/resource_2/<resource_2_pk>/`

    It allows to define all url kwargs and lookup fields to perform the lookup.
    """

    lookup_fields: list[str] = ["pk"]
    lookup_url_kwargs: list[str] = []

    def __getattribute__(self, item):
        """
        This method is overridden to allow to get the last lookup field or lookup url kwarg
        when accessing the `lookup_field` or `lookup_url_kwarg` attribute. This is useful
        to keep compatibility with all methods used by the parent class `GenericViewSet`.
        """
        if item in ["lookup_field", "lookup_url_kwarg"]:
            return getattr(self, item + "s", [None])[-1]

        return super().__getattribute__(item)

    def get_queryset(self):
        """
        Get the list of items for this view.

        `lookup_fields` attribute is enumerated here to perform the nested lookup.
        """
        queryset = super().get_queryset()

        # The last lookup field is removed to perform the nested lookup as it corresponds
        # to the object pk, it is used within get_object method.
        lookup_url_kwargs = (
            self.lookup_url_kwargs[:-1]
            if self.lookup_url_kwargs
            else self.lookup_fields[:-1]
        )

        filter_kwargs = {}
        for index, lookup_url_kwarg in enumerate(lookup_url_kwargs):
            if lookup_url_kwarg not in self.kwargs:
                raise KeyError(
                    f"Expected view {self.__class__.__name__} to be called with a URL "
                    f'keyword argument named "{lookup_url_kwarg}". Fix your URL conf, or '
                    "set the `.lookup_fields` attribute on the view correctly."
                )

            filter_kwargs.update(
                {self.lookup_fields[index]: self.kwargs[lookup_url_kwarg]}
            )

        return queryset.filter(**filter_kwargs)


class SerializerPerActionMixin:
    """
    A mixin to allow to define serializer classes for each action.

    This mixin is useful to avoid to define a serializer class for each action in the
    `get_serializer_class` method.

    Example:
    ```
    class MyViewSet(SerializerPerActionMixin, viewsets.GenericViewSet):
        serializer_class = MySerializer
        list_serializer_class = MyListSerializer
        retrieve_serializer_class = MyRetrieveSerializer
    ```
    """

    def get_serializer_class(self):
        """
        Return the serializer class to use depending on the action.
        """
        if serializer_class := getattr(self, f"{self.action}_serializer_class", None):
            return serializer_class
        return super().get_serializer_class()


class Pagination(drf.pagination.PageNumberPagination):
    """Pagination to display no more than 100 objects per page sorted by creation date."""

    ordering = "-created_on"
    max_page_size = 200
    page_size_query_param = "page_size"


class UserViewSet(
    drf.mixins.UpdateModelMixin, viewsets.GenericViewSet, drf.mixins.ListModelMixin
):
    """User ViewSet"""

    permission_classes = [permissions.IsSelf]
    queryset = models.User.objects.all()
    serializer_class = serializers.UserSerializer

    def get_queryset(self):
        """
        Limit listed users by querying the email field with a trigram similarity
        search if a query is provided.
        Limit listed users by excluding users already in the document if a document_id
        is provided.
        """
        queryset = self.queryset

        if self.action != "list":
            return queryset

        # Exclude all users already in the given document
        if document_id := self.request.GET.get("document_id", ""):
            queryset = queryset.exclude(documentaccess__document_id=document_id)

        if not (query := self.request.GET.get("q", "")):
            return queryset

        # For emails, match emails by Levenstein distance to prevent typing errors
        if "@" in query:
            return (
                queryset.annotate(
                    distance=RawSQL("levenshtein(email::text, %s::text)", (query,))
                )
                .filter(distance__lte=3)
                .order_by("distance", "email")
            )

        # Use trigram similarity for non-email-like queries
        # For performance reasons we filter first by similarity, which relies on an
        # index, then only calculate precise similarity scores for sorting purposes
        return (
            queryset.filter(email__trigram_word_similar=query)
            .annotate(similarity=TrigramSimilarity("email", query))
            .filter(similarity__gt=0.2)
            .order_by("-similarity", "email")
        )

    @drf.decorators.action(
        detail=False,
        methods=["get"],
        url_name="me",
        url_path="me",
        permission_classes=[permissions.IsAuthenticated],
    )
    def get_me(self, request):
        """
        Return information on currently logged user
        """
        context = {"request": request}
        return drf.response.Response(
            self.serializer_class(request.user, context=context).data
        )


class ResourceAccessViewsetMixin:
    """Mixin with methods common to all access viewsets."""

    def get_permissions(self):
        """User only needs to be authenticated to list resource accesses"""
        if self.action == "list":
            permission_classes = [permissions.IsAuthenticated]
        else:
            return super().get_permissions()

        return [permission() for permission in permission_classes]

    def get_serializer_context(self):
        """Extra context provided to the serializer class."""
        context = super().get_serializer_context()
        context["resource_id"] = self.kwargs["resource_id"]
        return context

    def get_queryset(self):
        """Return the queryset according to the action."""
        queryset = super().get_queryset()
        queryset = queryset.filter(
            **{self.resource_field_name: self.kwargs["resource_id"]}
        )

        if self.action == "list":
            user = self.request.user
            teams = user.teams
            user_roles_query = (
                queryset.filter(
                    db.Q(user=user) | db.Q(team__in=teams),
                    **{self.resource_field_name: self.kwargs["resource_id"]},
                )
                .values(self.resource_field_name)
                .annotate(roles_array=ArrayAgg("role"))
                .values("roles_array")
            )

            # Limit to resource access instances related to a resource THAT also has
            # a resource access
            # instance for the logged-in user (we don't want to list only the resource
            # access instances pointing to the logged-in user)
            queryset = (
                queryset.filter(
                    db.Q(**{f"{self.resource_field_name}__accesses__user": user})
                    | db.Q(
                        **{f"{self.resource_field_name}__accesses__team__in": teams}
                    ),
                    **{self.resource_field_name: self.kwargs["resource_id"]},
                )
                .annotate(user_roles=db.Subquery(user_roles_query))
                .distinct()
            )
        return queryset

    def destroy(self, request, *args, **kwargs):
        """Forbid deleting the last owner access"""
        instance = self.get_object()
        resource = getattr(instance, self.resource_field_name)

        # Check if the access being deleted is the last owner access for the resource
        if (
            instance.role == "owner"
            and resource.accesses.filter(role="owner").count() == 1
        ):
            return drf.response.Response(
                {"detail": "Cannot delete the last owner access for the resource."},
                status=drf.status.HTTP_403_FORBIDDEN,
            )

        return super().destroy(request, *args, **kwargs)

    def perform_update(self, serializer):
        """Check that we don't change the role if it leads to losing the last owner."""
        instance = serializer.instance

        # Check if the role is being updated and the new role is not "owner"
        if (
            "role" in self.request.data
            and self.request.data["role"] != models.RoleChoices.OWNER
        ):
            resource = getattr(instance, self.resource_field_name)
            # Check if the access being updated is the last owner access for the resource
            if (
                instance.role == models.RoleChoices.OWNER
                and resource.accesses.filter(role=models.RoleChoices.OWNER).count() == 1
            ):
                message = "Cannot change the role to a non-owner role for the last owner access."
                raise drf.exceptions.PermissionDenied({"detail": message})

        serializer.save()


class DocumentMetadata(drf.metadata.SimpleMetadata):
    """Custom metadata class to add information"""

    def determine_metadata(self, request, view):
        """Add language choices only for the list endpoint."""
        simple_metadata = super().determine_metadata(request, view)

        if request.path.endswith("/documents/"):
            simple_metadata["actions"]["POST"]["language"] = {
                "choices": [
                    {"value": code, "display_name": name}
                    for code, name in enums.ALL_LANGUAGES.items()
                ]
            }
        return simple_metadata


# pylint: disable=too-many-public-methods
class DocumentViewSet(
    SerializerPerActionMixin,
    drf.mixins.CreateModelMixin,
    drf.mixins.DestroyModelMixin,
    drf.mixins.ListModelMixin,
    drf.mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """
    DocumentViewSet API.

    This view set provides CRUD operations and additional actions for managing documents.
    Supports filtering, ordering, and annotations for enhanced querying capabilities.

    ### API Endpoints:
    1. **List**: Retrieve a paginated list of documents.
       Example: GET /documents/?page=2
    2. **Retrieve**: Get a specific document by its ID.
       Example: GET /documents/{id}/
    3. **Create**: Create a new document.
       Example: POST /documents/
    4. **Update**: Update a document by its ID.
       Example: PUT /documents/{id}/
    5. **Delete**: Soft delete a document by its ID.
       Example: DELETE /documents/{id}/

    ### Additional Actions:
    1. **Trashbin**: List soft deleted documents for a document owner
        Example: GET /documents/{id}/trashbin/

    2. **Children**: List or create child documents.
        Example: GET, POST /documents/{id}/children/

    3. **Versions List**: Retrieve version history of a document.
        Example: GET /documents/{id}/versions/

    4. **Version Detail**: Get or delete a specific document version.
        Example: GET, DELETE /documents/{id}/versions/{version_id}/

    5. **Favorite**: Get list of favorite documents for a user. Mark or unmark
        a document as favorite.
        Examples:
        - GET /documents/favorite/
        - POST, DELETE /documents/{id}/favorite/

    6. **Create for Owner**: Create a document via server-to-server on behalf of a user.
        Example: POST /documents/create-for-owner/

    7. **Link Configuration**: Update document link configuration.
        Example: PUT /documents/{id}/link-configuration/

    8. **Attachment Upload**: Upload a file attachment for the document.
        Example: POST /documents/{id}/attachment-upload/

    9. **Media Auth**: Authorize access to document media.
        Example: GET /documents/media-auth/

    10. **Collaboration Auth**: Authorize access to the collaboration server for a document.
        Example: GET /documents/collaboration-auth/

    11. **AI Transform**: Apply a transformation action on a piece of text with AI.
        Example: POST /documents/{id}/ai-transform/
        Expected data:
        - text (str): The input text.
        - action (str): The transformation type, one of [prompt, correct, rephrase, summarize].
        Returns: JSON response with the processed text.
        Throttled by: AIDocumentRateThrottle, AIUserRateThrottle.

    12. **AI Translate**: Translate a piece of text with AI.
        Example: POST /documents/{id}/ai-translate/
        Expected data:
        - text (str): The input text.
        - language (str): The target language, chosen from settings.LANGUAGES.
        Returns: JSON response with the translated text.
        Throttled by: AIDocumentRateThrottle, AIUserRateThrottle.

    ### Ordering: created_at, updated_at, is_favorite, title

        Example:
        - Ascending: GET /api/v1.0/documents/?ordering=created_at
        - Desceding: GET /api/v1.0/documents/?ordering=-title

    ### Filtering:
        - `is_creator_me=true`: Returns documents created by the current user.
        - `is_creator_me=false`: Returns documents created by other users.
        - `is_favorite=true`: Returns documents marked as favorite by the current user
        - `is_favorite=false`: Returns documents not marked as favorite by the current user
        - `title=hello`: Returns documents which title contains the "hello" string

        Example:
        - GET /api/v1.0/documents/?is_creator_me=true&is_favorite=true
        - GET /api/v1.0/documents/?is_creator_me=false&title=hello

    ### Annotations:
    1. **is_favorite**: Indicates whether the document is marked as favorite by the current user.
    2. **user_roles**: Roles the current user has on the document or its ancestors.

    ### Notes:
    - Only the highest ancestor in a document hierarchy is shown in list views.
    - Implements soft delete logic to retain document tree structures.
    """

    filter_backends = [drf_filters.DjangoFilterBackend]
    filterset_class = DocumentFilter
    metadata_class = DocumentMetadata
    ordering = ["-updated_at"]
    ordering_fields = ["created_at", "updated_at", "title"]
    permission_classes = [
        permissions.DocumentAccessPermission,
    ]
    queryset = models.Document.objects.all()
    serializer_class = serializers.DocumentSerializer
    list_serializer_class = serializers.ListDocumentSerializer
    trashbin_serializer_class = serializers.ListDocumentSerializer
    children_serializer_class = serializers.ListDocumentSerializer
    ai_translate_serializer_class = serializers.AITranslateSerializer

    def annotate_is_favorite(self, queryset):
        """
        Annotate document queryset with the favorite status for the current user.
        """
        user = self.request.user

        if user.is_authenticated:
            favorite_exists_subquery = models.DocumentFavorite.objects.filter(
                document_id=db.OuterRef("pk"), user=user
            )
            return queryset.annotate(is_favorite=db.Exists(favorite_exists_subquery))

        return queryset.annotate(is_favorite=db.Value(False))

    def annotate_user_roles(self, queryset):
        """
        Annotate document queryset with the roles of the current user
        on the document or its ancestors.
        """
        user = self.request.user
        output_field = ArrayField(base_field=db.CharField())

        if user.is_authenticated:
            user_roles_subquery = models.DocumentAccess.objects.filter(
                db.Q(user=user) | db.Q(team__in=user.teams),
                document__path=Left(db.OuterRef("path"), Length("document__path")),
            ).values_list("role", flat=True)

            return queryset.annotate(
                user_roles=db.Func(
                    user_roles_subquery, function="ARRAY", output_field=output_field
                )
            )

        return queryset.annotate(
            user_roles=db.Value([], output_field=output_field),
        )

    def get_queryset(self):
        """Get queryset performing all annotation and filtering on the document tree structure."""
        user = self.request.user
        queryset = super().get_queryset()

        # Only list views need filtering and annotation
        if self.detail:
            return queryset

        if not user.is_authenticated:
            return queryset.none()

        queryset = queryset.filter(ancestors_deleted_at__isnull=True)

        # Filter documents to which the current user has access...
        access_documents_ids = models.DocumentAccess.objects.filter(
            db.Q(user=user) | db.Q(team__in=user.teams)
        ).values_list("document_id", flat=True)

        # ...or that were previously accessed and are not restricted
        traced_documents_ids = models.LinkTrace.objects.filter(user=user).values_list(
            "document_id", flat=True
        )

        return queryset.filter(
            db.Q(id__in=access_documents_ids)
            | (
                db.Q(id__in=traced_documents_ids)
                & ~db.Q(link_reach=models.LinkReachChoices.RESTRICTED)
            )
        )

    def filter_queryset(self, queryset):
        """Apply annotations and filters sequentially."""
        filterset = DocumentFilter(
            self.request.GET, queryset=queryset, request=self.request
        )
        filterset.is_valid()
        filter_data = filterset.form.cleaned_data

        # Filter as early as possible on fields that are available on the model
        for field in ["is_creator_me", "title"]:
            queryset = filterset.filters[field].filter(queryset, filter_data[field])

        queryset = self.annotate_user_roles(queryset)

        if self.action == "list":
            # Among the results, we may have documents that are ancestors/descendants
            # of each other. In this case we want to keep only the highest ancestors.
            root_paths = utils.filter_root_paths(
                queryset.order_by("path").values_list("path", flat=True),
                skip_sorting=True,
            )
            queryset = queryset.filter(path__in=root_paths)

            # Annotate the queryset with an attribute marking instances as highest ancestor
            # in order to save some time while computing abilities in the instance
            queryset = queryset.annotate(
                is_highest_ancestor_for_user=db.Value(
                    True, output_field=db.BooleanField()
                )
            )

        # Annotate favorite status and filter if applicable as late as possible
        queryset = self.annotate_is_favorite(queryset)
        queryset = filterset.filters["is_favorite"].filter(
            queryset, filter_data["is_favorite"]
        )

        # Apply ordering only now that everyting is filtered and annotated
        return filters.OrderingFilter().filter_queryset(self.request, queryset, self)

    def get_response_for_queryset(self, queryset):
        """Return paginated response for the queryset if requested."""
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            result = self.get_paginated_response(serializer.data)
            return result

        serializer = self.get_serializer(queryset, many=True)
        return drf.response.Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        """
        Add a trace that the document was accessed by a user. This is used to list documents
        on a user's list view even though the user has no specific role in the document (link
        access when the link reach configuration of the document allows it).
        """
        user = self.request.user
        instance = self.get_object()
        serializer = self.get_serializer(instance)

        # The `create` query generates 5 db queries which are much less efficient than an
        # `exists` query. The user will visit the document many times after the first visit
        # so that's what we should optimize for.
        if (
            user.is_authenticated
            and not instance.link_traces.filter(user=user).exists()
        ):
            models.LinkTrace.objects.create(document=instance, user=request.user)

        return drf.response.Response(serializer.data)

    @transaction.atomic
    def perform_create(self, serializer):
        """Set the current user as creator and owner of the newly created object."""
        obj = models.Document.add_root(
            creator=self.request.user,
            **serializer.validated_data,
        )
        serializer.instance = obj
        models.DocumentAccess.objects.create(
            document=obj,
            user=self.request.user,
            role=models.RoleChoices.OWNER,
        )

    def perform_destroy(self, instance):
        """Override to implement a soft delete instead of dumping the record in database."""
        instance.soft_delete()

    @drf.decorators.action(
        detail=False,
        methods=["get"],
        permission_classes=[permissions.IsAuthenticated],
    )
    def favorite_list(self, request, *args, **kwargs):
        """Get list of favorite documents for the current user."""
        user = request.user

        favorite_documents_ids = models.DocumentFavorite.objects.filter(
            user=user
        ).values_list("document_id", flat=True)

        queryset = self.get_queryset()
        queryset = queryset.filter(id__in=favorite_documents_ids)
        return self.get_response_for_queryset(queryset)

    @drf.decorators.action(
        detail=False,
        methods=["get"],
    )
    def trashbin(self, request, *args, **kwargs):
        """
        Retrieve soft-deleted documents for which the current user has the owner role.

        The selected documents are those deleted within the cutoff period defined in the
        settings (see TRASHBIN_CUTOFF_DAYS), before they are considered permanently deleted.
        """
        queryset = self.queryset.filter(
            deleted_at__isnull=False,
            deleted_at__gte=models.get_trashbin_cutoff(),
        )
        queryset = self.annotate_user_roles(queryset)
        queryset = queryset.filter(user_roles__contains=[models.RoleChoices.OWNER])

        return self.get_response_for_queryset(queryset)

    @drf.decorators.action(
        authentication_classes=[authentication.ServerToServerAuthentication],
        detail=False,
        methods=["post"],
        permission_classes=[],
        url_path="create-for-owner",
    )
    def create_for_owner(self, request):
        """
        Create a document on behalf of a specified owner (pre-existing user or invited).
        """
        # Deserialize and validate the data
        serializer = serializers.ServerCreateDocumentSerializer(data=request.data)
        if not serializer.is_valid():
            return drf_response.Response(
                serializer.errors, status=status.HTTP_400_BAD_REQUEST
            )

        document = serializer.save()

        return drf_response.Response(
            {"id": str(document.id)}, status=status.HTTP_201_CREATED
        )

    @drf.decorators.action(detail=True, methods=["post"])
    @transaction.atomic
    def move(self, request, *args, **kwargs):
        """
        Move a document to another location within the document tree.

        The user must be an administrator or owner of both the document being moved
        and the target parent document.
        """
        user = request.user
        document = self.get_object()  # including permission checks

        # Validate the input payload
        serializer = serializers.MoveDocumentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated_data = serializer.validated_data

        target_document_id = validated_data["target_document_id"]
        try:
            target_document = models.Document.objects.get(
                id=target_document_id, ancestors_deleted_at__isnull=True
            )
        except models.Document.DoesNotExist:
            return drf.response.Response(
                {"target_document_id": "Target parent document does not exist."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        position = validated_data["position"]
        message = None

        if position in [
            enums.MoveNodePositionChoices.FIRST_CHILD,
            enums.MoveNodePositionChoices.LAST_CHILD,
        ]:
            if not target_document.get_abilities(user).get("move"):
                message = (
                    "You do not have permission to move documents "
                    "as a child to this target document."
                )
        elif not target_document.is_root():
            if not target_document.get_parent().get_abilities(user).get("move"):
                message = (
                    "You do not have permission to move documents "
                    "as a sibling of this target document."
                )

        if message:
            return drf.response.Response(
                {"target_document_id": message},
                status=status.HTTP_400_BAD_REQUEST,
            )

        document.move(target_document, pos=position)

        return drf.response.Response(
            {"message": "Document moved successfully."}, status=status.HTTP_200_OK
        )

    @drf.decorators.action(
        detail=True,
        methods=["post"],
    )
    def restore(self, request, *args, **kwargs):
        """
        Restore a soft-deleted document if it was deleted less than x days ago.
        """
        document = self.get_object()
        document.restore()

        return drf_response.Response(
            {"detail": "Document has been successfully restored."},
            status=status.HTTP_200_OK,
        )

    @drf.decorators.action(
        detail=True,
        methods=["get", "post"],
        ordering=["path"],
        url_path="children",
    )
    def children(self, request, *args, **kwargs):
        """Handle listing and creating children of a document"""
        document = self.get_object()

        if request.method == "POST":
            # Create a child document
            serializer = serializers.DocumentSerializer(
                data=request.data, context=self.get_serializer_context()
            )
            serializer.is_valid(raise_exception=True)

            with transaction.atomic():
                child_document = document.add_child(
                    creator=request.user,
                    **serializer.validated_data,
                )
                models.DocumentAccess.objects.create(
                    document=child_document,
                    user=request.user,
                    role=models.RoleChoices.OWNER,
                )
            # Set the created instance to the serializer
            serializer.instance = child_document

            headers = self.get_success_headers(serializer.data)
            return drf.response.Response(
                serializer.data, status=status.HTTP_201_CREATED, headers=headers
            )

        # GET: List children
        queryset = document.get_children().filter(deleted_at__isnull=True)
        queryset = self.filter_queryset(queryset)
        queryset = self.annotate_is_favorite(queryset)
        queryset = self.annotate_user_roles(queryset)
        return self.get_response_for_queryset(queryset)

    @drf.decorators.action(detail=True, methods=["get"], url_path="versions")
    def versions_list(self, request, *args, **kwargs):
        """
        Return the document's versions but only those created after the user got access
        to the document
        """
        user = request.user
        if not user.is_authenticated:
            raise drf.exceptions.PermissionDenied("Authentication required.")

        # Validate query parameters using dedicated serializer
        serializer = serializers.VersionFilterSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        document = self.get_object()

        # Users should not see version history dating from before they gained access to the
        # document. Filter to get the minimum access date for the logged-in user
        access_queryset = models.DocumentAccess.objects.filter(
            db.Q(user=user) | db.Q(team__in=user.teams),
            document__path=Left(db.Value(document.path), Length("document__path")),
        ).aggregate(min_date=db.Min("created_at"))

        # Handle the case where the user has no accesses
        min_datetime = access_queryset["min_date"]
        if not min_datetime:
            return drf.exceptions.PermissionDenied(
                "Only users with specific access can see version history"
            )

        versions_data = document.get_versions_slice(
            from_version_id=serializer.validated_data.get("version_id"),
            min_datetime=min_datetime,
            page_size=serializer.validated_data.get("page_size"),
        )

        return drf.response.Response(versions_data)

    @drf.decorators.action(
        detail=True,
        methods=["get", "delete"],
        url_path="versions/(?P<version_id>[0-9a-z-]+)",
    )
    # pylint: disable=unused-argument
    def versions_detail(self, request, pk, version_id, *args, **kwargs):
        """Custom action to retrieve a specific version of a document"""
        document = self.get_object()

        try:
            response = document.get_content_response(version_id=version_id)
        except (FileNotFoundError, ClientError) as err:
            raise Http404 from err

        # Don't let users access versions that were created before they were given access
        # to the document
        user = request.user
        min_datetime = min(
            access.created_at
            for access in models.DocumentAccess.objects.filter(
                db.Q(user=user) | db.Q(team__in=user.teams),
                document__path=Left(db.Value(document.path), Length("document__path")),
            )
        )

        if response["LastModified"] < min_datetime:
            raise Http404

        if request.method == "DELETE":
            response = document.delete_version(version_id)
            return drf.response.Response(
                status=response["ResponseMetadata"]["HTTPStatusCode"]
            )

        return drf.response.Response(
            {
                "content": response["Body"].read().decode("utf-8"),
                "last_modified": response["LastModified"],
                "id": version_id,
            }
        )

    @drf.decorators.action(detail=True, methods=["put"], url_path="link-configuration")
    def link_configuration(self, request, *args, **kwargs):
        """Update link configuration with specific rights (cf get_abilities)."""
        # Check permissions first
        document = self.get_object()

        # Deserialize and validate the data
        serializer = serializers.LinkDocumentSerializer(
            document, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)

        serializer.save()

        # Notify collaboration server about the link updated
        CollaborationService().reset_connections(str(document.id))

        return drf.response.Response(serializer.data, status=drf.status.HTTP_200_OK)

    @drf.decorators.action(detail=True, methods=["post", "delete"], url_path="favorite")
    def favorite(self, request, *args, **kwargs):
        """
        Mark or unmark the document as a favorite for the logged-in user based on the HTTP method.
        """
        # Check permissions first
        document = self.get_object()
        user = request.user

        if request.method == "POST":
            # Try to mark as favorite
            try:
                models.DocumentFavorite.objects.create(document=document, user=user)
            except ValidationError:
                return drf.response.Response(
                    {"detail": "Document already marked as favorite"},
                    status=drf.status.HTTP_200_OK,
                )
            return drf.response.Response(
                {"detail": "Document marked as favorite"},
                status=drf.status.HTTP_201_CREATED,
            )

        # Handle DELETE method to unmark as favorite
        deleted, _ = models.DocumentFavorite.objects.filter(
            document=document, user=user
        ).delete()
        if deleted:
            return drf.response.Response(
                {"detail": "Document unmarked as favorite"},
                status=drf.status.HTTP_204_NO_CONTENT,
            )
        return drf.response.Response(
            {"detail": "Document was already not marked as favorite"},
            status=drf.status.HTTP_200_OK,
        )

    @drf.decorators.action(detail=True, methods=["post"], url_path="attachment-upload")
    def attachment_upload(self, request, *args, **kwargs):
        """Upload a file related to a given document"""
        # Check permissions first
        document = self.get_object()

        # Validate metadata in payload
        serializer = serializers.FileUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Generate a generic yet unique filename to store the image in object storage
        file_id = uuid.uuid4()
        extension = serializer.validated_data["expected_extension"]

        # Prepare metadata for storage
        extra_args = {
            "Metadata": {"owner": str(request.user.id)},
            "ContentType": serializer.validated_data["content_type"],
        }
        file_unsafe = ""
        if serializer.validated_data["is_unsafe"]:
            extra_args["Metadata"]["is_unsafe"] = "true"
            file_unsafe = "-unsafe"

        key = f"{document.key_base}/{ATTACHMENTS_FOLDER:s}/{file_id!s}{file_unsafe}.{extension:s}"

        file_name = serializer.validated_data["file_name"]
        if (
            not serializer.validated_data["content_type"].startswith("image/")
            or serializer.validated_data["is_unsafe"]
        ):
            extra_args.update(
                {"ContentDisposition": f'attachment; filename="{file_name:s}"'}
            )
        else:
            extra_args.update(
                {"ContentDisposition": f'inline; filename="{file_name:s}"'}
            )

        file = serializer.validated_data["file"]
        default_storage.connection.meta.client.upload_fileobj(
            file, default_storage.bucket_name, key, ExtraArgs=extra_args
        )

        return drf.response.Response(
            {"file": f"{settings.MEDIA_URL:s}{key:s}"},
            status=drf.status.HTTP_201_CREATED,
        )

    def _authorize_subrequest(self, request, pattern):
        """
        Shared method to authorize access based on the original URL of an Nginx subrequest
        and user permissions. Returns a dictionary of URL parameters if authorized.

        The original url is passed by nginx in the "HTTP_X_ORIGINAL_URL" header.
        See corresponding ingress configuration in Helm chart and read about the
        nginx.ingress.kubernetes.io/auth-url annotation to understand how the Nginx ingress
        is configured to do this.

        Based on the original url and the logged in user, we must decide if we authorize Nginx
        to let this request go through (by returning a 200 code) or if we block it (by returning
        a 403 error). Note that we return 403 errors without any further details for security
        reasons.

        Parameters:
        - pattern: The regex pattern to extract identifiers from the URL.

        Returns:
        - A dictionary of URL parameters if the request is authorized.
        Raises:
        - PermissionDenied if authorization fails.
        """
        # Extract the original URL from the request header
        original_url = request.META.get("HTTP_X_ORIGINAL_URL")
        if not original_url:
            logger.debug("Missing HTTP_X_ORIGINAL_URL header in subrequest")
            raise drf.exceptions.PermissionDenied()

        parsed_url = urlparse(original_url)
        match = pattern.search(parsed_url.path)

        # If the path does not match the pattern, try to extract the parameters from the query
        if not match:
            match = pattern.search(parsed_url.query)

        if not match:
            logger.debug(
                "Subrequest URL '%s' did not match pattern '%s'",
                parsed_url.path,
                pattern,
            )
            raise drf.exceptions.PermissionDenied()

        try:
            url_params = match.groupdict()
        except (ValueError, AttributeError) as exc:
            logger.debug("Failed to extract parameters from subrequest URL: %s", exc)
            raise drf.exceptions.PermissionDenied() from exc

        pk = url_params.get("pk")
        if not pk:
            logger.debug("Document ID (pk) not found in URL parameters: %s", url_params)
            raise drf.exceptions.PermissionDenied()

        # Fetch the document and check if the user has access
        try:
            document = models.Document.objects.get(pk=pk)
        except models.Document.DoesNotExist as exc:
            logger.debug("Document with ID '%s' does not exist", pk)
            raise drf.exceptions.PermissionDenied() from exc

        user_abilities = document.get_abilities(request.user)

        if not user_abilities.get(self.action, False):
            logger.debug(
                "User '%s' lacks permission for document '%s'", request.user, pk
            )
            raise drf.exceptions.PermissionDenied()

        logger.debug(
            "Subrequest authorization successful. Extracted parameters: %s", url_params
        )
        return url_params, user_abilities, request.user.id

    @drf.decorators.action(detail=False, methods=["get"], url_path="media-auth")
    def media_auth(self, request, *args, **kwargs):
        """
        This view is used by an Nginx subrequest to control access to a document's
        attachment file.

        When we let the request go through, we compute authorization headers that will be added to
        the request going through thanks to the nginx.ingress.kubernetes.io/auth-response-headers
        annotation. The request will then be proxied to the object storage backend who will
        respond with the file after checking the signature included in headers.
        """
        url_params, _, _ = self._authorize_subrequest(
            request, MEDIA_STORAGE_URL_PATTERN
        )
        pk, key = url_params.values()

        # Generate S3 authorization headers using the extracted URL parameters
        request = utils.generate_s3_authorization_headers(f"{pk:s}/{key:s}")

        return drf.response.Response("authorized", headers=request.headers, status=200)

    @drf.decorators.action(detail=False, methods=["get"], url_path="collaboration-auth")
    def collaboration_auth(self, request, *args, **kwargs):
        """
        This view is used by an Nginx subrequest to control access to a document's
        collaboration server.
        """
        _, user_abilities, user_id = self._authorize_subrequest(
            request, COLLABORATION_WS_URL_PATTERN
        )
        can_edit = user_abilities["partial_update"]

        # Add the collaboration server secret token to the headers
        headers = {
            "Authorization": settings.COLLABORATION_SERVER_SECRET,
            "X-Can-Edit": str(can_edit),
            "X-User-Id": str(user_id),
        }

        return drf.response.Response("authorized", headers=headers, status=200)

    @drf.decorators.action(
        detail=True, methods=["get"], url_path="collaboration-auth-jwt"
    )
    def collaboration_auth_jwt(self, request, *args, **kwargs):
        """
        This view is used to issue a short-lived JWT token for the collaboration server
        to be used as a Bearer token in the Authorization header.
        """

        document = self.get_object()

        user_abilities = document.get_abilities(request.user)
        can_edit = user_abilities["partial_update"]

        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            minutes=5
        )

        jwt_token = jwt.encode(
            {
                "user_id": str(request.user.id),
                "can_edit": can_edit,
                "document_id": str(document.id),
                "exp": expires_at,
            },
            settings.COLLABORATION_SERVER_SECRET,
            algorithm="HS256",
        )

        return drf.response.Response({"token": jwt_token}, status=200)

    @drf.decorators.action(
        detail=True,
        methods=["post"],
        name="Apply a transformation action on a piece of text with AI",
        url_path="ai-transform",
        throttle_classes=[utils.AIDocumentRateThrottle, utils.AIUserRateThrottle],
    )
    def ai_transform(self, request, *args, **kwargs):
        """
        POST /api/v1.0/documents/<resource_id>/ai-transform
        with expected data:
        - text: str
        - action: str [prompt, correct, rephrase, summarize]
        Return JSON response with the processed text.
        """
        # Check permissions first
        self.get_object()

        serializer = serializers.AITransformSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        text = serializer.validated_data["text"]
        action = serializer.validated_data["action"]

        response = AIService().transform(text, action)

        return drf.response.Response(response, status=drf.status.HTTP_200_OK)

    @drf.decorators.action(
        detail=True,
        methods=["post"],
        name="Translate a piece of text with AI",
        url_path="ai-translate",
        throttle_classes=[utils.AIDocumentRateThrottle, utils.AIUserRateThrottle],
    )
    def ai_translate(self, request, *args, **kwargs):
        """
        POST /api/v1.0/documents/<resource_id>/ai-translate
        with expected data:
        - text: str
        - language: str [settings.LANGUAGES]
        Return JSON response with the translated text.
        """
        # Check permissions first
        self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        text = serializer.validated_data["text"]
        language = serializer.validated_data["language"]

        response = AIService().translate(text, language)

        return drf.response.Response(response, status=drf.status.HTTP_200_OK)


class DocumentAccessViewSet(
    ResourceAccessViewsetMixin,
    drf.mixins.CreateModelMixin,
    drf.mixins.DestroyModelMixin,
    drf.mixins.ListModelMixin,
    drf.mixins.RetrieveModelMixin,
    drf.mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """
    API ViewSet for all interactions with document accesses.

    GET /api/v1.0/documents/<resource_id>/accesses/:<document_access_id>
        Return list of all document accesses related to the logged-in user or one
        document access if an id is provided.

    POST /api/v1.0/documents/<resource_id>/accesses/ with expected data:
        - user: str
        - role: str [administrator|editor|reader]
        Return newly created document access

    PUT /api/v1.0/documents/<resource_id>/accesses/<document_access_id>/ with expected data:
        - role: str [owner|admin|editor|reader]
        Return updated document access

    PATCH /api/v1.0/documents/<resource_id>/accesses/<document_access_id>/ with expected data:
        - role: str [owner|admin|editor|reader]
        Return partially updated document access

    DELETE /api/v1.0/documents/<resource_id>/accesses/<document_access_id>/
        Delete targeted document access
    """

    lookup_field = "pk"
    pagination_class = Pagination
    permission_classes = [permissions.IsAuthenticated, permissions.AccessPermission]
    queryset = models.DocumentAccess.objects.select_related("user").all()
    resource_field_name = "document"
    serializer_class = serializers.DocumentAccessSerializer

    def perform_create(self, serializer):
        """Add a new access to the document and send an email to the new added user."""
        access = serializer.save()

        access.document.send_invitation_email(
            access.user.email,
            access.role,
            self.request.user,
            access.user.language
            or self.request.user.language
            or settings.LANGUAGE_CODE,
        )

    def perform_update(self, serializer):
        """Update an access to the document and notify the collaboration server."""
        access = serializer.save()

        access_user_id = None
        if access.user:
            access_user_id = str(access.user.id)

        # Notify collaboration server about the access change
        CollaborationService().reset_connections(
            str(access.document.id), access_user_id
        )

    def perform_destroy(self, instance):
        """Delete an access to the document and notify the collaboration server."""
        instance.delete()

        # Notify collaboration server about the access removed
        CollaborationService().reset_connections(
            str(instance.document.id), str(instance.user.id)
        )


class TemplateViewSet(
    drf.mixins.CreateModelMixin,
    drf.mixins.DestroyModelMixin,
    drf.mixins.RetrieveModelMixin,
    drf.mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Template ViewSet"""

    filter_backends = [drf.filters.OrderingFilter]
    permission_classes = [
        permissions.IsAuthenticatedOrSafe,
        permissions.AccessPermission,
    ]
    ordering = ["-created_at"]
    ordering_fields = ["created_at", "updated_at", "title"]
    serializer_class = serializers.TemplateSerializer
    queryset = models.Template.objects.all()

    def get_queryset(self):
        """Custom queryset to get user related templates."""
        queryset = super().get_queryset()
        user = self.request.user

        if not user.is_authenticated:
            return queryset

        user_roles_query = (
            models.TemplateAccess.objects.filter(
                db.Q(user=user) | db.Q(team__in=user.teams),
                template_id=db.OuterRef("pk"),
            )
            .values("template")
            .annotate(roles_array=ArrayAgg("role"))
            .values("roles_array")
        )
        return queryset.annotate(user_roles=db.Subquery(user_roles_query)).distinct()

    def list(self, request, *args, **kwargs):
        """Restrict templates returned by the list endpoint"""
        queryset = self.filter_queryset(self.get_queryset())
        user = self.request.user
        if user.is_authenticated:
            queryset = queryset.filter(
                db.Q(accesses__user=user)
                | db.Q(accesses__team__in=user.teams)
                | db.Q(is_public=True)
            )
        else:
            queryset = queryset.filter(is_public=True)

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return drf.response.Response(serializer.data)

    @transaction.atomic
    def perform_create(self, serializer):
        """Set the current user as owner of the newly created object."""
        obj = serializer.save()
        models.TemplateAccess.objects.create(
            template=obj,
            user=self.request.user,
            role=models.RoleChoices.OWNER,
        )


class TemplateAccessViewSet(
    ResourceAccessViewsetMixin,
    drf.mixins.CreateModelMixin,
    drf.mixins.DestroyModelMixin,
    drf.mixins.ListModelMixin,
    drf.mixins.RetrieveModelMixin,
    drf.mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """
    API ViewSet for all interactions with template accesses.

    GET /api/v1.0/templates/<template_id>/accesses/:<template_access_id>
        Return list of all template accesses related to the logged-in user or one
        template access if an id is provided.

    POST /api/v1.0/templates/<template_id>/accesses/ with expected data:
        - user: str
        - role: str [administrator|editor|reader]
        Return newly created template access

    PUT /api/v1.0/templates/<template_id>/accesses/<template_access_id>/ with expected data:
        - role: str [owner|admin|editor|reader]
        Return updated template access

    PATCH /api/v1.0/templates/<template_id>/accesses/<template_access_id>/ with expected data:
        - role: str [owner|admin|editor|reader]
        Return partially updated template access

    DELETE /api/v1.0/templates/<template_id>/accesses/<template_access_id>/
        Delete targeted template access
    """

    lookup_field = "pk"
    pagination_class = Pagination
    permission_classes = [permissions.IsAuthenticated, permissions.AccessPermission]
    queryset = models.TemplateAccess.objects.select_related("user").all()
    resource_field_name = "template"
    serializer_class = serializers.TemplateAccessSerializer


class InvitationViewset(
    drf.mixins.CreateModelMixin,
    drf.mixins.ListModelMixin,
    drf.mixins.RetrieveModelMixin,
    drf.mixins.DestroyModelMixin,
    drf.mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """API ViewSet for user invitations to document.

    GET /api/v1.0/documents/<document_id>/invitations/:<invitation_id>/
        Return list of invitations related to that document or one
        document access if an id is provided.

    POST /api/v1.0/documents/<document_id>/invitations/ with expected data:
        - email: str
        - role: str [administrator|editor|reader]
        Return newly created invitation (issuer and document are automatically set)

    PATCH /api/v1.0/documents/<document_id>/invitations/:<invitation_id>/ with expected data:
        - role: str [owner|admin|editor|reader]
        Return partially updated document invitation

    DELETE  /api/v1.0/documents/<document_id>/invitations/<invitation_id>/
        Delete targeted invitation
    """

    lookup_field = "id"
    pagination_class = Pagination
    permission_classes = [
        permissions.CanCreateInvitationPermission,
        permissions.AccessPermission,
    ]
    queryset = (
        models.Invitation.objects.all()
        .select_related("document")
        .order_by("-created_at")
    )
    serializer_class = serializers.InvitationSerializer

    def get_serializer_context(self):
        """Extra context provided to the serializer class."""
        context = super().get_serializer_context()
        context["resource_id"] = self.kwargs["resource_id"]
        return context

    def get_queryset(self):
        """Return the queryset according to the action."""
        queryset = super().get_queryset()
        queryset = queryset.filter(document=self.kwargs["resource_id"])

        if self.action == "list":
            user = self.request.user
            teams = user.teams

            # Determine which role the logged-in user has in the document
            user_roles_query = (
                models.DocumentAccess.objects.filter(
                    db.Q(user=user) | db.Q(team__in=teams),
                    document=self.kwargs["resource_id"],
                )
                .values("document")
                .annotate(roles_array=ArrayAgg("role"))
                .values("roles_array")
            )

            queryset = (
                # The logged-in user should be administrator or owner to see its accesses
                queryset.filter(
                    db.Q(
                        document__accesses__user=user,
                        document__accesses__role__in=models.PRIVILEGED_ROLES,
                    )
                    | db.Q(
                        document__accesses__team__in=teams,
                        document__accesses__role__in=models.PRIVILEGED_ROLES,
                    ),
                )
                # Abilities are computed based on logged-in user's role and
                # the user role on each document access
                .annotate(user_roles=db.Subquery(user_roles_query))
                .distinct()
            )
        return queryset

    def perform_create(self, serializer):
        """Save invitation to a document then send an email to the invited user."""
        invitation = serializer.save()

        invitation.document.send_invitation_email(
            invitation.email,
            invitation.role,
            self.request.user,
            self.request.user.language or settings.LANGUAGE_CODE,
        )


class ConfigView(drf.views.APIView):
    """API ViewSet for sharing some public settings."""

    permission_classes = [AllowAny]

    def get(self, request):
        """
        GET /api/v1.0/config/
            Return a dictionary of public settings.
        """
        array_settings = [
            "COLLABORATION_WS_URL",
            "CRISP_WEBSITE_ID",
            "ENVIRONMENT",
            "FRONTEND_THEME",
            "MEDIA_BASE_URL",
            "POSTHOG_KEY",
            "LANGUAGES",
            "LANGUAGE_CODE",
            "SENTRY_DSN",
        ]
        dict_settings = {}
        for setting in array_settings:
            if hasattr(settings, setting):
                dict_settings[setting] = getattr(settings, setting)

        return drf.response.Response(dict_settings)

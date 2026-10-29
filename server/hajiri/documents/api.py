"""
Documents & letters JSON API for the React app (cookie session, frontend/src/api/documents.js) and Bearer-token
clients: the TS routes under /api/v1/documents with their response shapes ({categories}, {documents}, {letters}, ...).
DRF views with SameOriginAuthentication, the TS error shape and JsonRenderer; each adds the feature switch and, where
the TS route did, the HR role. Rules and validation stay in services.py; every query is scoped to the active company.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from hajiri.core.api import FeatureEnabled, HasRole, IsSignedIn, SameOriginAuthentication
from hajiri.files.store import stored_file_response

from . import serializers as s
from . import services
from .views import letter_response

# The React app sends the session cookie with fetch() and no CSRF token: the TS sameOriginGuard (Origin check) applies
# instead. The feature switch is checked before the role, as the TS featureGate ran in front of the router.
AUTH = [SameOriginAuthentication]
FEATURE = FeatureEnabled('documents')
SIGNED_IN = [IsSignedIn, FEATURE]
HR = [IsSignedIn, FEATURE, HasRole('hr')]


def _query(request) -> dict:
    return request.query_params.dict()


def _created(body: dict) -> Response:
    return Response(body, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- categories

@extend_schema(methods=['GET'], responses=s.DocumentCategoryOut(many=True))
@extend_schema(methods=['POST'], request=s.DocumentCategoryIn, responses={201: s.DocumentIdOut})
@api_view(['GET', 'POST'])
@authentication_classes(AUTH)
@permission_classes(SIGNED_IN)
def categories(request):
    if request.method == 'POST':
        return _created(services.create_category(request.user, request.data))
    return Response({'categories': services.list_categories(request.user)})


@extend_schema(request=s.DocumentCategoryPatchIn, responses=s.DocumentOkOut)
@api_view(['PATCH'])
@authentication_classes(AUTH)
@permission_classes(HR)
def category(request, category_id):
    return Response(services.update_category(request.user, category_id, request.data))


# ---------------------------------------------------------------- employee documents

@extend_schema(responses=s.DocumentOut(many=True))
@api_view(['GET'])
@authentication_classes(AUTH)
@permission_classes(SIGNED_IN)
def mine(request):
    return Response({'documents': services.my_documents(request.user)})


@extend_schema(methods=['GET'], parameters=[s.DocumentHistoryQuery], responses=s.DocumentOut(many=True))
@extend_schema(methods=['POST'], request=s.DocumentIn, responses={201: s.DocumentIdOut})
@api_view(['GET', 'POST'])
@authentication_classes(AUTH)
@permission_classes(SIGNED_IN)
def employee(request, employee_id):
    if request.method == 'POST':
        return _created(services.add_document(request.user, employee_id, request.data))
    return Response({'documents': services.employee_documents(request.user, employee_id, _query(request))})


@extend_schema(parameters=[s.DocumentQueueQuery], responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
@permission_classes(HR)
def queue(request):
    return Response(services.queue(request.user, _query(request)))


@extend_schema(methods=['PATCH'], request=s.DocumentPatchIn, responses=s.DocumentOkOut)
@extend_schema(methods=['DELETE'], request=None, responses=s.DocumentOkOut)
@api_view(['PATCH', 'DELETE'])
@authentication_classes(AUTH)
@permission_classes(SIGNED_IN)
def document(request, doc_id):
    if request.method == 'DELETE':
        return Response(services.delete_document(request.user, doc_id))
    return Response(services.update_document(request.user, doc_id, request.data))


@extend_schema(responses={(200, 'application/octet-stream'): bytes})
@api_view(['GET'])
@authentication_classes(AUTH)
@permission_classes(SIGNED_IN)
def document_file(request, doc_id):
    f = services.document_file(request.user, doc_id)
    return stored_file_response(f, download=request.query_params.get('download') == '1')


@extend_schema(request=s.DocumentVerifyIn, responses=s.DocumentOkOut)
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes(HR)
def verify(request, doc_id):
    return Response(services.verify_document(request.user, doc_id, request.data))


# ---------------------------------------------------------------- letter settings and templates

@extend_schema(responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
@permission_classes(HR)
def placeholders(request):
    return Response({'placeholders': services.placeholders(request.user)})


@extend_schema(methods=['GET'], responses=dict)
@extend_schema(methods=['PUT'], request=s.LetterSettingsIn, responses=dict)
@api_view(['GET', 'PUT'])
@authentication_classes(AUTH)
@permission_classes(HR)
def letter_settings(request):
    if request.method == 'PUT':
        return Response({'settings': services.save_letter_settings(request.user, request.data)})
    return Response({'settings': services.get_letter_settings(request.user)})


@extend_schema(methods=['GET'], responses=dict)
@extend_schema(methods=['POST'], request=s.LetterTemplateIn, responses={201: s.DocumentIdOut})
@api_view(['GET', 'POST'])
@authentication_classes(AUTH)
@permission_classes(SIGNED_IN)
def templates(request):
    if request.method == 'POST':
        return _created(services.create_template(request.user, request.data))
    return Response({'templates': services.list_templates(request.user)})


@extend_schema(request=s.LetterTemplatePatchIn, responses=s.DocumentOkOut)
@api_view(['PATCH'])
@authentication_classes(AUTH)
@permission_classes(HR)
def template(request, template_id):
    return Response(services.update_template(request.user, template_id, request.data))


@extend_schema(request=s.LetterPreviewIn, responses=s.LetterPreviewOut)
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes(HR)
def template_preview(request):
    return Response(services.preview_letter(request.user, request.data))


# ---------------------------------------------------------------- letters

@extend_schema(methods=['GET'], parameters=[s.LettersQuery], responses=dict)
@extend_schema(methods=['POST'], request=s.LetterIssueIn, responses={201: s.LetterIssuedOut})
@api_view(['GET', 'POST'])
@authentication_classes(AUTH)
@permission_classes(SIGNED_IN)
def letters(request):
    if request.method == 'POST':
        return _created(services.issue_letter(request.user, request.data))
    return Response({'letters': services.list_letters(request.user, _query(request))})


@extend_schema(responses={(200, 'text/html'): str})
@api_view(['GET'])
@authentication_classes(AUTH)
@permission_classes(SIGNED_IN)
def letter_html(request, letter_id):
    return letter_response(services.letter_html(request.user, letter_id))


@extend_schema(request=s.LetterVoidIn, responses=s.DocumentOkOut)
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes(HR)
def void(request, letter_id):
    return Response(services.void_letter(request.user, letter_id, request.data))


@extend_schema(methods=['GET'], parameters=[s.RequestsQuery], responses=dict)
@extend_schema(methods=['POST'], request=s.LetterRequestIn, responses={201: s.DocumentIdOut})
@api_view(['GET', 'POST'])
@authentication_classes(AUTH)
@permission_classes(SIGNED_IN)
def letter_requests(request):
    if request.method == 'POST':
        return _created(services.request_letter(request.user, request.data))
    return Response({'requests': services.list_requests(request.user, _query(request))})


@extend_schema(request=None, responses=s.DocumentOkOut)
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes(SIGNED_IN)
def letter_request_cancel(request, request_id):
    return Response(services.cancel_request(request.user, request_id))


# ---------------------------------------------------------------- policies

@extend_schema(methods=['GET'], parameters=[s.PolicyArchivedQuery], responses=dict)
@extend_schema(methods=['POST'], request=s.CompanyPolicyIn, responses={201: s.DocumentIdOut})
@api_view(['GET', 'POST'])
@authentication_classes(AUTH)
@permission_classes(SIGNED_IN)
def policies(request):
    if request.method == 'POST':
        return _created(services.publish_policy(request.user, request.data))
    return Response({'policies': services.list_policies(request.user, _query(request))})


@extend_schema(request=s.CompanyPolicyPatchIn, responses=s.DocumentOkOut)
@api_view(['PATCH'])
@authentication_classes(AUTH)
@permission_classes(HR)
def policy(request, policy_id):
    return Response(services.update_policy(request.user, policy_id, request.data))


@extend_schema(responses={(200, 'application/octet-stream'): bytes})
@api_view(['GET'])
@authentication_classes(AUTH)
@permission_classes(SIGNED_IN)
def policy_file(request, policy_id):
    f = services.policy_file(request.user, policy_id)
    return stored_file_response(f, download=request.query_params.get('download') == '1')


@extend_schema(request=None, responses=s.DocumentOkOut)
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes(SIGNED_IN)
def acknowledge(request, policy_id):
    return Response(services.acknowledge_policy(request.user, policy_id))


@extend_schema(responses=dict)
@api_view(['GET'])
@authentication_classes(AUTH)
@permission_classes(HR)
def policy_acks(request, policy_id):
    return Response({'employees': services.policy_acks(request.user, policy_id)})


@extend_schema(request=None, responses=s.PolicyRemindOut)
@api_view(['POST'])
@authentication_classes(AUTH)
@permission_classes(HR)
def policy_remind(request, policy_id):
    return Response(services.remind_policy(request.user, policy_id))

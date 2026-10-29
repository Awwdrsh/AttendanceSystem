"""
Engagement JSON API for the React app and Bearer-token clients: the TS routes under /api/v1/engage (routes.ts), with
the same paths, bodies, answers and status codes. DRF views with the project defaults (hajiri.core.api:
IsSignedIn, the TS error shape, JsonRenderer) and SameOriginAuthentication (the session cookie the React app sends
with fetch(), checked against the Origin like the TS sameOriginGuard, or a Bearer token), plus the feature switches
the TS router had in front of it (featureGate 'kudos' on /kudos and /leaderboard, 'surveys' on /surveys, 'suggestions' on /suggestions).
The services check roles and company scope themselves (with the TS messages) and validate the bodies, so the views
stay thin. Ids are any path segment: the services answer a malformed one with 400 'Invalid UUID', as uuid.parse did.
"""
from __future__ import annotations

from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import FeatureEnabled, IsSignedIn, SameOriginAuthentication

from . import services

CREATED = status.HTTP_201_CREATED


def _query(request) -> dict:
    return request.query_params.dict()


class EngageView(APIView):
    authentication_classes = [SameOriginAuthentication]
    permission_classes = [IsSignedIn]


class KudosFeatureView(EngageView):
    permission_classes = [IsSignedIn, FeatureEnabled('kudos')]


class SurveysFeatureView(EngageView):
    permission_classes = [IsSignedIn, FeatureEnabled('surveys')]


class SuggestionsFeatureView(EngageView):
    permission_classes = [IsSignedIn, FeatureEnabled('suggestions')]


# ---------------------------------------------------------------- reactions and comments

class ReactionsView(EngageView):
    @extend_schema(request=dict, responses=dict)
    def post(self, request):
        out = services.react(request.user, request.data)
        # The TS answer was {reacted, reactions, mine}; the comment count is for the HTMX bar only.
        return Response({'reacted': out['reacted'], 'reactions': out['reactions'], 'mine': out['mine']})


class CommentsView(EngageView):
    @extend_schema(operation_id='api_v1_engage_comments_list', responses=dict)
    def get(self, request):
        return Response({'comments': services.list_comments(request.user, _query(request))})

    @extend_schema(request=dict, responses={201: dict})
    def post(self, request):
        out = services.add_comment(request.user, request.data)
        return Response({'id': out['id']}, status=CREATED)


class CommentView(EngageView):
    @extend_schema(responses=dict)
    def delete(self, request, id):
        services.delete_comment(request.user, id)
        return Response({'ok': True})


# ---------------------------------------------------------------- announcements

class AnnouncementsView(EngageView):
    @extend_schema(operation_id='api_v1_engage_announcements_list', responses=dict)
    def get(self, request):
        return Response({'announcements': services.list_announcements(request.user, _query(request))})

    @extend_schema(request=dict, responses={201: dict})
    def post(self, request):
        return Response(services.post_announcement(request.user, request.data), status=CREATED)


class AnnouncementView(EngageView):
    @extend_schema(request=dict, responses=dict)
    def patch(self, request, id):
        return Response(services.edit_announcement(request.user, id, request.data))

    @extend_schema(responses=dict)
    def delete(self, request, id):
        return Response(services.delete_announcement(request.user, id))


class AnnouncementAckView(EngageView):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        return Response(services.ack_announcement(request.user, id))


class AnnouncementAcksView(EngageView):
    @extend_schema(responses=dict)
    def get(self, request, id):
        out = services.announcement_acks(request.user, id)
        return Response({'audience': out['audience'], 'acked': out['acked'], 'people': out['people']})


# ---------------------------------------------------------------- company values and settings

class ValuesView(EngageView):
    @extend_schema(operation_id='api_v1_engage_values_list', responses=dict)
    def get(self, request):
        return Response(services.list_values(request.user))

    @extend_schema(request=dict, responses={201: dict})
    def post(self, request):
        return Response(services.create_value(request.user, request.data), status=CREATED)


class ValueView(EngageView):
    @extend_schema(request=dict, responses=dict)
    def patch(self, request, id):
        return Response(services.update_value(request.user, id, request.data))


class SettingsView(EngageView):
    @extend_schema(request=dict, responses=dict)
    def put(self, request):
        return Response(services.save_settings(request.user, request.data))


# ---------------------------------------------------------------- kudos

class ColleaguesView(EngageView):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response({'colleagues': services.colleagues(request.user)})


class KudosListView(KudosFeatureView):
    @extend_schema(operation_id='api_v1_engage_kudos_list', responses=dict)
    def get(self, request):
        return Response({'kudos': services.list_kudos(request.user, _query(request))})

    @extend_schema(request=dict, responses={201: dict})
    def post(self, request):
        return Response(services.give_kudos(request.user, request.data), status=CREATED)


class KudosView(KudosFeatureView):
    @extend_schema(responses=dict)
    def delete(self, request, id):
        return Response(services.delete_kudos(request.user, id))


class LeaderboardView(KudosFeatureView):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.leaderboard(request.user, _query(request)))


# ---------------------------------------------------------------- feed, celebrations, preferences, home card

class FeedView(EngageView):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.feed(request.user, _query(request)))


class CelebrationsView(EngageView):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response({'celebrations': services.celebrations_for(request.user, _query(request))})


class PrefsView(EngageView):
    @extend_schema(operation_id='api_v1_engage_me_prefs_read', responses=dict)
    def get(self, request):
        return Response(services.get_prefs(request.user))

    @extend_schema(request=dict, responses=dict)
    def put(self, request):
        return Response(services.save_prefs(request.user, request.data))


class HomeView(EngageView):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.home(request.user))


# ---------------------------------------------------------------- surveys and polls

class SurveysView(SurveysFeatureView):
    @extend_schema(operation_id='api_v1_engage_surveys_list', responses=dict)
    def get(self, request):
        return Response({'surveys': services.open_surveys_for(request.user)})

    @extend_schema(request=dict, responses={201: dict})
    def post(self, request):
        return Response(services.create_survey(request.user, request.data), status=CREATED)


class SurveysManageView(SurveysFeatureView):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response({'surveys': services.manage_surveys(request.user)})


class SurveyView(SurveysFeatureView):
    @extend_schema(operation_id='api_v1_engage_survey_read', responses=dict)
    def get(self, request, id):
        return Response(services.get_survey(request.user, id))

    @extend_schema(request=dict, responses=dict)
    def put(self, request, id):
        return Response(services.update_survey(request.user, id, request.data))

    @extend_schema(responses=dict)
    def delete(self, request, id):
        return Response(services.delete_survey(request.user, id))


class SurveyPublishView(SurveysFeatureView):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        return Response(services.publish_survey(request.user, id))


class SurveyCloseView(SurveysFeatureView):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        return Response(services.close_survey(request.user, id))


class SurveyRespondView(SurveysFeatureView):
    @extend_schema(request=dict, responses={201: dict})
    def post(self, request, id):
        return Response(services.respond(request.user, id, request.data), status=CREATED)


class SurveyResultsView(SurveysFeatureView):
    @extend_schema(responses=dict)
    def get(self, request, id):
        return Response(services.results(request.user, id))


class SurveyResultsCsvView(SurveysFeatureView):
    @extend_schema(responses={(200, 'text/csv'): str})
    def get(self, request, id):
        resp = HttpResponse(services.results_csv(request.user, id), content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = 'attachment; filename="survey-results.csv"'
        return resp


# ---------------------------------------------------------------- anonymous suggestion box

class SuggestionsView(SuggestionsFeatureView):
    @extend_schema(operation_id='api_v1_engage_suggestions_list', responses=dict)
    def get(self, request):
        return Response({'suggestions': services.list_suggestions(request.user, _query(request))})

    @extend_schema(request=dict, responses={201: dict})
    def post(self, request):
        return Response(services.suggest(request.user, request.data), status=CREATED)


class PublicSuggestionsView(SuggestionsFeatureView):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response({'suggestions': services.public_suggestions(request.user)})


class SuggestionView(SuggestionsFeatureView):
    @extend_schema(request=dict, responses=dict)
    def patch(self, request, id):
        return Response(services.update_suggestion(request.user, id, request.data))

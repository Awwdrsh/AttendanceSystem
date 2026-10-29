from django.urls import path

# JSON API for the React app and Bearer-token clients (TS: /api/v1/engage/...). Ids are <str:...> so the services
# answer a malformed one with 400 'Invalid UUID', as the TS uuid.parse did.
from . import api

E = 'api/v1/engage'
urlpatterns = [
    path(f'{E}/reactions', api.ReactionsView.as_view(), name='api_reactions'),
    path(f'{E}/comments', api.CommentsView.as_view(), name='api_comments'),
    path(f'{E}/comments/<str:id>', api.CommentView.as_view(), name='api_comment'),
    path(f'{E}/announcements', api.AnnouncementsView.as_view(), name='api_announcements'),
    path(f'{E}/announcements/<str:id>', api.AnnouncementView.as_view(), name='api_announcement'),
    path(f'{E}/announcements/<str:id>/ack', api.AnnouncementAckView.as_view(), name='api_announcement_ack'),
    path(f'{E}/announcements/<str:id>/acks', api.AnnouncementAcksView.as_view(), name='api_announcement_acks'),
    path(f'{E}/values', api.ValuesView.as_view(), name='api_values'),
    path(f'{E}/values/<str:id>', api.ValueView.as_view(), name='api_value'),
    path(f'{E}/settings', api.SettingsView.as_view(), name='api_settings'),
    path(f'{E}/colleagues', api.ColleaguesView.as_view(), name='api_colleagues'),
    path(f'{E}/kudos', api.KudosListView.as_view(), name='api_kudos_list'),
    path(f'{E}/kudos/<str:id>', api.KudosView.as_view(), name='api_kudos'),
    path(f'{E}/leaderboard', api.LeaderboardView.as_view(), name='api_leaderboard'),
    path(f'{E}/feed', api.FeedView.as_view(), name='api_feed'),
    path(f'{E}/celebrations', api.CelebrationsView.as_view(), name='api_celebrations'),
    path(f'{E}/me/prefs', api.PrefsView.as_view(), name='api_prefs'),
    path(f'{E}/home', api.HomeView.as_view(), name='api_home'),
    path(f'{E}/surveys', api.SurveysView.as_view(), name='api_surveys'),
    path(f'{E}/surveys/manage', api.SurveysManageView.as_view(), name='api_surveys_manage'),
    path(f'{E}/surveys/<str:id>', api.SurveyView.as_view(), name='api_survey'),
    path(f'{E}/surveys/<str:id>/publish', api.SurveyPublishView.as_view(), name='api_survey_publish'),
    path(f'{E}/surveys/<str:id>/close', api.SurveyCloseView.as_view(), name='api_survey_close'),
    path(f'{E}/surveys/<str:id>/respond', api.SurveyRespondView.as_view(), name='api_survey_respond'),
    path(f'{E}/surveys/<str:id>/results', api.SurveyResultsView.as_view(), name='api_survey_results'),
    path(f'{E}/surveys/<str:id>/results.csv', api.SurveyResultsCsvView.as_view(), name='api_survey_results_csv'),
    path(f'{E}/suggestions', api.SuggestionsView.as_view(), name='api_suggestions'),
    path(f'{E}/suggestions/public', api.PublicSuggestionsView.as_view(), name='api_suggestions_public'),
    path(f'{E}/suggestions/<str:id>', api.SuggestionView.as_view(), name='api_suggestion'),
]

from django.urls import path

from . import views

urlpatterns = [
    # pages (hubs.tsx EngageHub)
    path('engage', views.feed_page, name='feed'),
    path('engage/surveys', views.surveys_page, name='surveys'),
    path('engage/suggestions', views.suggestions_page, name='suggestions'),
    # feed and side panels
    path('_/engage/feed', views.feed_partial, name='feed_partial'),
    path('_/engage/celebrations', views.celebrations_panel, name='celebrations'),
    path('_/engage/prefs', views.save_prefs, name='prefs'),
    path('_/engage/leaderboard', views.leaderboard_panel, name='leaderboard'),
    path('_/engage/suggestion-box', views.suggestion_panel, name='suggestion_box'),
    path('_/engage/home', views.home_card, name='home_card'),
    # reactions and comments
    path('_/engage/react', views.react, name='react'),
    path('_/engage/comments', views.comments, name='comments'),
    path('_/engage/comments/<uuid:id>/delete', views.comment_delete, name='comment_delete'),
    # kudos, values, settings
    path('_/engage/kudos/new', views.kudos_new, name='kudos_new'),
    path('_/engage/kudos/<uuid:id>/delete', views.kudos_delete, name='kudos_delete'),
    path('_/engage/values', views.values_manager, name='values'),
    path('_/engage/values/new', views.value_new, name='value_new'),
    path('_/engage/values/<uuid:id>/toggle', views.value_toggle, name='value_toggle'),
    path('_/engage/settings', views.save_settings, name='settings'),
    # announcements
    path('_/engage/announcements/new', views.announcement_new, name='announcement_new'),
    path('_/engage/announcements/<uuid:id>/pin', views.announcement_pin, name='announcement_pin'),
    path('_/engage/announcements/<uuid:id>/delete', views.announcement_delete, name='announcement_delete'),
    path('_/engage/announcements/<uuid:id>/ack', views.announcement_ack, name='announcement_ack'),
    path('_/engage/announcements/<uuid:id>/acks', views.announcement_acks, name='announcement_acks'),
    # surveys
    path('_/engage/surveys', views.surveys_partial, name='surveys_partial'),
    path('_/engage/surveys/new', views.survey_new, name='survey_new'),
    path('_/engage/surveys/<uuid:id>/edit', views.survey_edit, name='survey_edit'),
    path('_/engage/surveys/<uuid:id>/publish', views.survey_publish, name='survey_publish'),
    path('_/engage/surveys/<uuid:id>/close', views.survey_close, name='survey_close'),
    path('_/engage/surveys/<uuid:id>/delete', views.survey_delete, name='survey_delete'),
    path('_/engage/surveys/<uuid:id>/closing-day', views.survey_extend_form, name='survey_extend_form'),
    path('_/engage/surveys/<uuid:id>/extend', views.survey_extend, name='survey_extend'),
    path('_/engage/surveys/<uuid:id>/answer', views.survey_answer, name='survey_answer'),
    path('_/engage/surveys/<uuid:id>/results', views.survey_results, name='survey_results'),
    path('_/engage/surveys/<uuid:id>/results.csv', views.survey_results_csv, name='survey_results_csv'),
    # suggestion box
    path('_/engage/suggestions/new', views.suggestion_new, name='suggestion_new'),
    path('_/engage/suggestions/<uuid:id>/update', views.suggestion_update, name='suggestion_update'),
    path('_/engage/suggestions/<uuid:id>/reply', views.suggestion_reply, name='suggestion_reply'),
]
